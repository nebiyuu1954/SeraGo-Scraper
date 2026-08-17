"""GraphQL (Hasura) scraper — used for Afriwork.

HaHuJobs is a second GraphQL site; it reuses this pipeline from its own
module (``core/scrapers/hahujobs.py``) and is dispatched by slug in the
ScraperFactory. This module stays Afriwork-specific for the detail row.

Fetches one page via POST with ``query`` + variables, then extracts the
results list at the configured ``results_path`` (e.g. ``data.jobs``).

When ``only_today`` is enabled and the pagination rules declare a
``date_filter`` (field + from/to variable names), the request variables get
``from``/``to`` bounds covering the current local day, so the server only
returns today's listings.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

import httpx
from django.utils import timezone

from core.models import AfriworkJob, AfriworkScrapeLog, ScrapedItem

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


class GraphQLScraper(BaseScraper):
    """Paged POST/JSON scraper for Hasura GraphQL endpoints."""

    site_log_model = AfriworkScrapeLog
    #: Per-site detail model + the OneToOne link on ScrapedItem — lets the
    #: shared batch-save path upsert every detail row in one statement.
    detail_model = AfriworkJob
    detail_fk_field = "afriwork_job"

    def _today_range(self) -> tuple[datetime, datetime]:
        """Today's local-day window as aware datetimes: [00:00, tomorrow 00:00)."""
        today = timezone.localdate()
        tz = timezone.get_current_timezone()
        start = datetime.combine(today, dtime.min, tzinfo=tz)
        return start, start + timedelta(days=1)

    def _build_payload(self, page: int) -> dict:
        """Build the POST body for a given 0-based page.

        ``limit``/``offset`` are always derived from the page so multi-page
        runs advance correctly; any static values in ``pagination.variables``
        are ignored for these keys. Custom variable names can be configured
        via ``limit_var``/``offset_var`` in the pagination rules.

        When a ``date_filter`` is configured, ``from``/``to`` are always
        injected (the query requires them): today's local-day window when
        ``only_today`` is on, a wide 2000–2100 window when it's off.
        """
        pagination = self.source.pagination or {}
        page_size = int(pagination.get("page_size", DEFAULT_PAGE_SIZE))
        offset = page * page_size

        variables = dict(pagination.get("variables") or {})
        variables[pagination.get("limit_var", "limit")] = page_size
        variables[pagination.get("offset_var", "offset")] = offset

        date_filter = pagination.get("date_filter")
        if isinstance(date_filter, dict) and date_filter.get("field"):
            tz = timezone.get_current_timezone()
            if self.only_today:
                date_from, date_to = self._today_range()
            else:
                date_from = datetime(2000, 1, 1, tzinfo=tz)
                date_to = datetime(2100, 1, 1, tzinfo=tz)
            variables[date_filter.get("from_var", "from")] = date_from.isoformat()
            variables[date_filter.get("to_var", "to")] = date_to.isoformat()
        return {"query": self.source.query, "variables": variables}

    def fetch(self, page: int = 0) -> dict:
        payload = self._build_payload(page)
        headers = {"Content-Type": "application/json", **(self.source.headers or {})}

        response = request_with_retry(
            httpx.post,
            url=self.source.endpoint,
            json=payload,
            headers=headers,
            timeout=float((self.source.pagination or {}).get("timeout", DEFAULT_TIMEOUT)),
            retries=int((self.source.pagination or {}).get("retries", DEFAULT_RETRIES)),
        )
        # Record the request even when it fails — it still hit the API.
        self._record_api_call(page, response.status_code)
        response.raise_for_status()
        return response.json()

    _NESTED_LIST_NODE = {"skill_requirements": "skill", "sectors": "sector"}

    @classmethod
    def _nested_names(cls, raw: dict, key: str) -> list[str]:
        """Extract names from Afriwork's nested lists, e.g. skill_requirements/sectors.

        ``[{"skill": {"name": "Canva", "id": "..."}}]`` -> ``["Canva"]``.
        """
        names = []
        for entry in raw.get(key) or []:
            if not isinstance(entry, dict):
                continue
            node = entry.get(cls._NESTED_LIST_NODE.get(key, "") or "")
            if isinstance(node, dict) and node.get("name"):
                names.append(node["name"])
        return names

    def _detail_defaults(self, item: dict, instance: ScrapedItem) -> dict:
        """The AfriworkJob field values for a listing (a faithful mirror of the raw payload)."""
        raw = item.get("raw_data") or {}
        city = raw.get("city") or {}
        country = (city.get("country") or {}).get("name") or ""
        entity = raw.get("entity") or {}
        return {
            "title": raw.get("title") or item.get("title") or "",
            "description": item.get("description") or "",
            "location": item.get("location") or city.get("name") or "",
            "country": country,
            "job_type": item.get("job_type") or "",
            "job_site": raw.get("job_site") or "",
            "experience_level": raw.get("experience_level") or "",
            "approval_status": raw.get("approval_status") or "",
            "published_at": item.get("published_at"),
            "deadline": item.get("deadline"),
            "api_created_at": transform_parse_datetime(raw.get("created_at")),
            "api_updated_at": transform_parse_datetime(raw.get("updated_at")),
            "refreshed_at": transform_parse_datetime(raw.get("refreshed_at")),
            "entity_type": entity.get("type") or "",
            "entity_name": entity.get("name") or "",
            "skills": self._nested_names(raw, "skill_requirements"),
            "sectors": self._nested_names(raw, "sectors"),
            "compensation_amount_cents": raw.get("compensation_amount_cents"),
            "compensation_type": raw.get("compensation_type") or "",
            "compensation_currency": raw.get("compensation_currency") or "",
            "raw_payload": raw,
            "job_number": instance.job_number,
            "numbered_on": instance.numbered_on,
        }

    def _save_detail(self, item: dict, instance: ScrapedItem) -> None:
        """Create/update the AfriworkJob detail row and link it to the master.

        Persists EVERY field the Afriwork API returns for a listing, so the
        per-site model is a faithful mirror of the raw response (the raw JSON
        is also kept verbatim in ``raw_payload``).
        """
        afriwork, _ = AfriworkJob.objects.update_or_create(
            external_id=instance.external_id,
            defaults=self._detail_defaults(item, instance),
        )
        if instance.afriwork_job_id != afriwork.pk:
            ScrapedItem.objects.filter(pk=instance.pk).update(afriwork_job=afriwork)

    def parse(self, raw: dict) -> list[dict]:
        # Hasura reports GraphQL errors in the body with HTTP 200 — surface them.
        if isinstance(raw, dict) and raw.get("errors"):
            raise ScrapeError(f"GraphQL errors: {raw['errors']}")

        results_path = (self.source.pagination or {}).get("results_path", "data")
        items = dig(raw, results_path)
        if not isinstance(items, list):
            raise ScrapeError(f"Expected a list at '{results_path}', got {type(items).__name__}")
        return items


class AfriworkJobsScraper(GraphQLScraper):
    """Afriwork — GraphQL site. Adds the listing detail URL to the master row.

    The API payload has no URL field, but the site's job pages live at
    ``https://afriworket.com/jobs/<id>`` (the API's own opaque job id —
    verified against live URLs). Registered per-slug in the factory.

    Also owns the client-side today guard: Afriwork is the one GraphQL site
    whose server-side ``published_at`` filter can fail to constrain (a wide
    ``--no-today`` window, a refresh wave, an API change), so pre-today items
    are dropped here and the sweep stops at the first page with nothing from
    today. HaHu reuses the GraphQL pipeline but filters server-side on
    ``approved_on`` — it must NOT inherit these hooks (its all-ethiojobs
    pages would look like past-today pages and truncate the sweep), which is
    why they live on this subclass rather than the generic GraphQLScraper.
    """

    def normalize(self, raw_item: dict) -> dict:
        item = super().normalize(raw_item)
        if not item.get("url") and raw_item.get("id"):
            item["url"] = f"https://afriworket.com/jobs/{raw_item['id']}"
        # The API returns compensation as structured fields (cents + currency +
        # frequency) rather than a salary string — format it into the shared
        # ``salary`` text so the master row (and SeraGo's sync) carries the
        # money instead of losing it.
        cents = raw_item.get("compensation_amount_cents")
        currency = (raw_item.get("compensation_currency") or "").strip().upper()
        if cents and currency:
            amount = int(cents) / 100
            frequency = {
                "MONTHLY": "monthly",
                "ANNUALLY": "annually",
                "YEARLY": "yearly",
                "WEEKLY": "weekly",
                "DAILY": "daily",
                "HOURLY": "hourly",
                "FIXED": "",
                "ONE_TIME": "",
            }.get(
                (raw_item.get("compensation_type") or "").strip().upper(),
                "",
            )
            item["salary"] = f"{amount:,.0f} {currency} {frequency}".strip()
        return item

    # -- client-side today guard (mirrors RestJsonScraper) --

    def _today_start(self) -> datetime:
        """Aware datetime for the start of the current local day."""
        return timezone.make_aware(datetime.combine(timezone.localdate(), dtime.min))

    def _is_today_item(self, item: dict) -> bool:
        """True when the listing counts as 'today' by Afriwork's own rule.

        Mirrors the GraphQL query: a listing is today's when it was published
        today OR refreshed (reposted) today. A listing with no usable date at
        all is kept (safer than silently dropping it).
        """
        today_start = self._today_start()
        published = transform_parse_datetime(item.get("published_at"))
        if published is not None and published >= today_start:
            return True
        refreshed = transform_parse_datetime((item.get("raw_data") or {}).get("refreshed_at"))
        if refreshed is not None and refreshed >= today_start:
            return True
        return published is None and refreshed is None

    def _keep_item(self, item: dict) -> bool:
        """Drop listings that are not today's when the today filter is on."""
        if not self.only_today:
            return True
        return self._is_today_item(item)

    def _past_today_boundary(self, page: int, items: list[dict]) -> bool:
        """Stop when this page has already moved past today's listings.

        The API returns listings newest-first, so if a page contains NO items
        from today, every page below it is older than today too and the sweep
        can end. (Mixed pages are swept through and their pre-today items
        dropped by :meth:`_keep_item`.)
        """
        if not items:
            return True
        return all(not self._is_today_item(i) for i in items)
