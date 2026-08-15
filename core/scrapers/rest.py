"""REST JSON scraper — used for EthioJobs and similar GET/paginated APIs.

Fetches one page via GET with ``page``/``limit`` query params and reads the
list at the configured ``results_path`` (e.g. ``data``).

Two pagination conventions are supported, configured in the source's
``pagination`` rules:

* ``page_1_based: true``  — the API numbers pages from 1 (EthioJobs does).
* default                  — the API uses 0-based page/offset semantics.

When ``only_today`` is enabled and the pagination rules declare a
``date_filter`` WITHOUT ``from_var``/``to_var``, the API cannot filter by
date server-side, so the scraper stops the sweep client-side: the listings
arrive newest-first and once a page is entirely older than today the sweep
ends (see :meth:`_past_today_boundary`).
"""
from __future__ import annotations

from datetime import date, datetime

import httpx
from django.conf import settings
from django.utils import timezone

from core.models import EthioJobsJob, EthioJobsScrapeLog, ScrapedItem

from .base import (
    DEFAULT_RETRIES,
    BaseScraper,
    ScrapeError,
    dig,
    request_with_retry,
    transform_parse_datetime,
)

DEFAULT_PAGE_SIZE = 10
DEFAULT_TIMEOUT = 30.0


class RestJsonScraper(BaseScraper):
    """Paged GET/JSON scraper for REST APIs with a configurable results path."""

    site_log_model = EthioJobsScrapeLog
    #: Per-site detail model + the OneToOne link on ScrapedItem — lets the
    #: shared batch-save path upsert every detail row in one statement.
    detail_model = EthioJobsJob
    detail_fk_field = "ethiojobs_job"

    # -- pagination helpers --

    def _query_params(self, page: int) -> dict:
        """Query params for the given 0-based page index."""
        pagination = self.source.pagination or {}
        page_size = int(pagination.get("page_size", DEFAULT_PAGE_SIZE))

        params = dict(pagination.get("params") or {})
        if pagination.get("page_1_based"):
            params[pagination.get("page_key", "page")] = page + 1
        else:
            params[pagination.get("page_key", "page")] = page
        params[pagination.get("limit_key", "limit")] = page_size
        return params

    def _request_headers(self) -> dict:
        """Source headers plus the JWT token when configured in settings."""
        headers = {"Content-Type": "application/json", **(self.source.headers or {})}
        token = getattr(settings, "ETHIOJOBS_TOKEN", "") or ""
        if token:
            headers["x-custom-header"] = token
        return headers

    def fetch(self, page: int = 0) -> dict:
        params = self._query_params(page)

        response = request_with_retry(
            httpx.get,
            url=self.source.endpoint,
            params=params,
            headers=self._request_headers(),
            timeout=float((self.source.pagination or {}).get("timeout", DEFAULT_TIMEOUT)),
            retries=int((self.source.pagination or {}).get("retries", DEFAULT_RETRIES)),
        )
        # Record the request even when it fails — it still hit the API.
        self._record_api_call(page, response.status_code)
        response.raise_for_status()
        return response.json()

    def _today_start(self) -> datetime:
        """Aware datetime for the start of the current local day."""
        return timezone.make_aware(datetime.combine(timezone.localdate(), datetime.min.time()))

    def _is_today_item(self, item: dict) -> bool:
        """True when the item was published today (local time)."""
        date_filter = (self.source.pagination or {}).get("date_filter") or {}
        field = date_filter.get("field") or "published_at"
        published = transform_parse_datetime(item.get(field))
        if published is None:
            # No date at all: keep it (safer than silently dropping).
            return True
        return published >= self._today_start()

    def _keep_item(self, item: dict) -> bool:
        """Drop items published before today when the today filter is on."""
        if not self.only_today:
            return True
        return self._is_today_item(item)

    def _past_today_boundary(self, page: int, items: list[dict]) -> bool:
        """Stop when this page has already moved past today's listings.

        The API cannot filter by date, but returns listings newest-first, so
        if a page contains NO items from today, every page below it is older
        than today too and the sweep can end. (Mixed pages are swept through
        and their pre-today items dropped by :meth:`_keep_item`.)
        """
        if not items:
            return True  # everything kept was pre-today -> past the boundary
        return all(not self._is_today_item(i) for i in items)

    # -- parsing + detail saving --

    def parse(self, raw: dict) -> list[dict]:
        results_path = (self.source.pagination or {}).get("results_path", "data")
        items = dig(raw, results_path)
        if not isinstance(items, list):
            raise ScrapeError(f"Expected a list at '{results_path}', got {type(items).__name__}")
        return items

    def normalize(self, raw_item: dict) -> dict:
        item = super().normalize(raw_item)
        # The API payload has no detail URL, but the site's job pages live at
        # https://www.ethiojobs.net/jobs/<slug> — the same slug used as the
        # dedup external_id (verified against the site's own URLs).
        slug = raw_item.get("slug") or item.get("external_id")
        if not item.get("url") and slug:
            item["url"] = f"https://www.ethiojobs.net/jobs/{slug}"
        return item

    def _detail_defaults(self, item: dict, instance: ScrapedItem) -> dict:
        """The EthioJobsJob field values for a listing (a faithful mirror of the raw payload)."""
        raw = item.get("raw_data") or {}
        company = raw.get("company") or {}
        return {
            "api_id": raw.get("id") or "",
            "title": raw.get("title") or item.get("title") or "",
            "slug": raw.get("slug") or "",
            "description": item.get("description") or "",
            "state": raw.get("state") or item.get("location") or "",
            "type": raw.get("type"),
            "level": raw.get("level") or "",
            "location_type": raw.get("location_type") or "",
            "published_at": item.get("published_at"),
            "deadline": transform_parse_datetime(raw.get("date_expiry")),
            "catalogs": raw.get("catalogs") or [],
            "company": company,
            "application_method": raw.get("application_method") or "",
            "application_email": raw.get("application_email") or "",
            "career_page_link": raw.get("career_page_link") or "",
            "application_form": raw.get("application_form"),
            "raw_payload": raw,
            "job_number": instance.job_number,
            "numbered_on": instance.numbered_on,
        }

    def _save_detail(self, item: dict, instance: ScrapedItem) -> None:
        """Create/update the EthioJobsJob detail row and link it to the master.

        Persists EVERY field the EthioJobs REST API returns for a listing,
        so the per-site model is a faithful mirror of the raw response (the
        raw JSON is also kept verbatim in ``raw_payload``).
        """
        ethiojobs, _ = EthioJobsJob.objects.update_or_create(
            external_id=instance.external_id,
            defaults=self._detail_defaults(item, instance),
        )
        if instance.ethiojobs_job_id != ethiojobs.pk:
            ScrapedItem.objects.filter(pk=instance.pk).update(ethiojobs_job=ethiojobs)
