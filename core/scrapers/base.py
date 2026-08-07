"""Base scraper machinery for SeraGo.

Pipeline: fetch -> parse -> normalize (field mapping) -> save. Subclasses
only implement :meth:`fetch` and :meth:`parse`; everything else is
configuration-driven through the Source's ``field_mapping`` and
``pagination`` JSON.

Per-day job numbers are assigned chronologically (``#01`` = the first job
posted that day, ``#N`` = the newest so far; later scrapes append new jobs
with the next numbers and stop as soon as they reach a record already
stored).

Logging is split into exactly two levels (see core/models.py):
* the MASTER ``ScrapeLog`` — one row per day with the day's totals and a
  compact ``websites`` JSON (short numbers per website + table/log_id
  references to each website's own log), and
* a per-website log (e.g. ``AfriworkScrapeLog``) — one row per (site, day)
  holding the full per-run detail (pages hit, http statuses, errors).
Each finished run appends its summary to both. Every HTTP request is
counted in ``api_hits``/``pages_hit``.
"""
from __future__ import annotations

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Any, Callable

from bs4 import BeautifulSoup
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import SITE_LOG_MODELS, ScrapeLog, ScrapedItem, ScrapeStatus, Source

logger = logging.getLogger(__name__)

# Sort key fallback for items without a published date (they sort last).
_MIN_DATETIME = datetime.min.replace(tzinfo=dt_timezone.utc)


class ScrapeError(Exception):
    """Raised when a source returns unexpected data."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def dig(obj: Any, path: str | None) -> Any:
    """Resolve a dotted path (``"data.jobs"``) against nested dicts.

    Returns ``None`` when any segment is missing or the walk hits a non-dict.
    """
    if path is None:
        return None
    current = obj
    for part in str(path).split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def sha256_hex(*values: Any) -> str:
    """SHA-256 hex digest of the given values joined with ``|``."""
    joined = "|".join("" if value is None else str(value) for value in values)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Field transforms (referenced by name in a source's field_mapping)
# ---------------------------------------------------------------------------


def transform_strip_html(value: Any) -> str | None:
    """Strip HTML tags, unescape entities and collapse whitespace."""
    if value is None:
        return None
    text = BeautifulSoup(str(value), "html.parser").get_text(" ")
    return " ".join(text.split()) or None


def transform_clean_text(value: Any) -> str | None:
    """Collapse internal whitespace and trim."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def transform_upper(value: Any) -> str | None:
    """Uppercase a cleaned text value (normalizes job_type enums)."""
    cleaned = transform_clean_text(value)
    return cleaned.upper() if cleaned else None


def transform_parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (with or without timezone) into an aware datetime."""
    if value in (None, ""):
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = timezone.make_aware(parsed)
    return parsed


TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "strip_html": transform_strip_html,
    "clean_text": transform_clean_text,
    "upper": transform_upper,
    "parse_datetime": transform_parse_datetime,
}


# ---------------------------------------------------------------------------
# BaseScraper
# ---------------------------------------------------------------------------


class BaseScraper(ABC):
    """Configuration-driven scraper pipeline shared by every scraper type."""

    def __init__(self, source: Source):
        self.source = source
        self.only_today = bool(source.only_today)
        # Every HTTP request made by the current run, in order.
        self.api_calls: list[dict] = []

    # -- per-day numbering helpers --

    def _next_job_number(self) -> int:
        """Next sequential number for today's batch on this source (01, 02, ...)."""
        today = timezone.localdate()
        last = (
            ScrapedItem.objects.filter(source=self.source, numbered_on=today)
            .order_by("-job_number")
            .values_list("job_number", flat=True)
            .first()
        )
        return (last or 0) + 1

    def _insert_item(self, external_id: str, defaults: dict) -> ScrapedItem | None:
        """Insert one new item with today's next job number.

        Returns the created instance, or None if the insert kept racing with
        a concurrent worker (duplicate external_id or job_number).
        """
        for _ in range(3):
            insert_data = dict(defaults)
            insert_data["numbered_on"] = timezone.localdate()
            insert_data["job_number"] = self._next_job_number()
            try:
                # Own atomic block so the IntegrityError catch below stays safe
                # even if the caller wrapped the pipeline in a transaction
                # (e.g. TestCase, Celery task wrapping scrape() in atomic).
                with transaction.atomic():
                    return ScrapedItem.objects.create(
                        source=self.source, external_id=external_id, **insert_data
                    )
            except IntegrityError:
                continue
        return None

    # -- stages implemented by concrete scrapers --

    @abstractmethod
    def fetch(self, page: int = 0) -> Any:
        """Fetch one page of raw data. ``page`` is 0-based."""

    @abstractmethod
    def parse(self, raw: Any) -> list[dict]:
        """Extract a list of raw item dicts from the fetched payload."""

    # -- hooks for concrete scrapers (defaults: no-ops) --

    def _record_api_call(self, page: int, http_status: int) -> None:
        """Record one HTTP request on ``self.api_calls`` (called by fetch())."""
        self.api_calls.append(
            {
                "page": page,
                "http_status": http_status,
                "requested_at": timezone.now().isoformat(),
            }
        )

    def _save_detail(self, item: dict, instance: ScrapedItem) -> None:
        """Persist site-specific details for a newly inserted/updated item."""

    def record_detail_log(self, run: dict, day: date) -> str | None:
        """Append this run to the per-website day log; return its pk (or None).

        Subclasses override this to write their site-specific log (e.g.
        AfriworkScrapeLog). The returned pk is stored on the master log's
        ``websites`` bucket as ``log_id`` so the master references it.
        """
        return None

    # -- stages implemented here (config-driven) --

    def normalize(self, raw_item: dict) -> dict:
        """Map a raw item to normalized fields using the source's field_mapping.

        Each mapping value is either a dotted path string (``"title"``) or a
        spec dict: ``{"path": "description", "transforms": ["strip_html"]}``.
        """
        mapping = self.source.field_mapping or {}
        normalized: dict[str, Any] = {}
        for target, spec in mapping.items():
            if isinstance(spec, dict):
                path = spec.get("path")
                transforms = spec.get("transforms", [])
            else:
                path = spec
                transforms = []
            value = dig(raw_item, path)
            for name in transforms:
                if name in TRANSFORMS:
                    value = TRANSFORMS[name](value)
            normalized[target] = value
        return normalized

    def content_hash(self, item: dict) -> str:
        """Dedup fingerprint: SHA-256 of title|company|location|job_type."""
        return sha256_hex(
            item.get("title"),
            item.get("company"),
            item.get("location"),
            item.get("job_type"),
        )

    @staticmethod
    def _sort_key(item: dict) -> tuple[bool, datetime, str]:
        """Sort key: published time ascending (first posted = #01).

        Items without a date sort last; external_id breaks ties.
        """
        published = item.get("published_at")
        return (
            published is None,
            published or _MIN_DATETIME,
            str(item.get("external_id") or ""),
        )

    def _today_known_ids(self) -> set[str]:
        """External ids already stored for today — the incremental stop boundary."""
        today = timezone.localdate()
        return set(
            ScrapedItem.objects.filter(source=self.source, numbered_on=today).values_list(
                "external_id", flat=True
            )
        )

    def _incremental_stop_safe(self) -> bool:
        """True when today's scrapes of this source all completed cleanly.

        Reads the per-website day log (via the SITE_LOG_MODELS registry): its
        worst status is only ``success`` when EVERY run today succeeded, so
        the stored id set is a complete snapshot of the day. If any run
        failed or was truncated (PARTIAL), fall back to a full sweep.
        """
        site_log = self._site_log_for_day(timezone.localdate())
        return site_log is not None and site_log.status == ScrapeStatus.SUCCESS

    def _page_items(self, page: int) -> tuple[list[dict], int | None]:
        """Fetch + normalize one page. Returns (items, http_status)."""
        raw = self.fetch(page)
        http_status = self.api_calls[-1]["http_status"] if self.api_calls else None

        items: list[dict] = []
        for raw_item in self.parse(raw):
            item = self.normalize(raw_item)
            item["raw_data"] = raw_item
            items.append(item)
        return items, http_status

    def _run_page(self, page: int) -> dict:
        """One page: fetch -> parse -> normalize -> save. Returns stats."""
        items, http_status = self._page_items(page)
        # Number chronologically regardless of response order: #01 is the
        # first job posted that day, even if the API returns pages newest-first.
        items.sort(key=self._sort_key)

        inserted, updated, skipped, errors = self.save_items(items)
        return {
            "page": page,
            "http_status": http_status,
            "found": len(items),
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
        }

    def save_items(self, items: list[dict]) -> tuple[int, int, int, list[str]]:
        """Upsert normalized items, deduplicating on (source, external_id).

        Returns (inserted, updated, skipped, errors). Per-item failures are
        collected so one bad item never kills the run.
        """
        inserted = updated = skipped = 0
        errors: list[str] = []
        for item in items:
            try:
                external_id = str(item.get("external_id") or "").strip()
                if not external_id:
                    raise ScrapeError("Item is missing 'external_id'")

                content_hash = self.content_hash(item)
                defaults = {
                    key: value
                    for key, value in item.items()
                    if key not in ("external_id",) and value is not None
                }
                defaults["content_hash"] = content_hash
                status, instance = self._upsert(external_id, content_hash, defaults)
                if status == "inserted":
                    inserted += 1
                    self._save_detail(item, instance)
                elif status == "updated":
                    updated += 1
                    self._save_detail(item, instance)
                else:  # skipped
                    skipped += 1
            except Exception as exc:  # noqa: BLE001 - keep the run going
                errors.append(f"{item.get('external_id', '?')}: {exc}")
        return inserted, updated, skipped, errors

    def _upsert(
        self,
        external_id: str,
        content_hash: str,
        defaults: dict,
    ) -> tuple[str, ScrapedItem]:
        """Insert or update one item, deduplicating on (source, external_id).

        Returns (status, instance) where status is one of
        ``inserted`` / ``updated`` / ``skipped``.
        """
        existing = (
            ScrapedItem.objects.filter(source=self.source, external_id=external_id)
            .only("content_hash")
            .first()
        )
        if existing is None:
            instance = self._insert_item(external_id, defaults)
            if instance is not None:
                return "inserted", instance
            # Lost the race repeatedly — reload and treat as an existing row.
            existing = (
                ScrapedItem.objects.filter(source=self.source, external_id=external_id)
                .only("content_hash")
                .first()
            )
            if existing is None:
                raise ScrapeError(f"{external_id}: insert failed after retries")

        if existing.content_hash == content_hash:
            # Seen again, unchanged: refresh last_seen_at, count as skipped.
            ScrapedItem.objects.filter(pk=existing.pk).update(last_seen_at=timezone.now())
            return "skipped", existing

        # Note: fields mapped to None are intentionally absent from
        # ``defaults`` so flaky source fields never wipe good data. The
        # per-day job_number/numbered_on are assigned once at insert and
        # are never touched on update.
        for key, value in defaults.items():
            setattr(existing, key, value)
        existing.save(update_fields=list(defaults) + ["updated_at"])
        return "updated", existing

    # -- run orchestration --

    _STATUS_WEIGHT = {
        ScrapeStatus.FAILED: 3,
        ScrapeStatus.PARTIAL: 2,
        ScrapeStatus.SUCCESS: 1,
        ScrapeStatus.RUNNING: 0,
    }

    @classmethod
    def _worst_status(cls, *statuses: str | None) -> str:
        """The most severe status among the given ones (failed > partial > success)."""
        worst = ScrapeStatus.SUCCESS
        weight = -1
        for status in statuses:
            if status and cls._STATUS_WEIGHT.get(status, 0) > weight:
                weight = cls._STATUS_WEIGHT[status]
                worst = status
        return worst

    @staticmethod
    def _run_summary(run: dict) -> dict:
        """JSON-safe summary of one scrape run, for the day-level rollups."""
        return {
            "status": run.get("status"),
            "page": run.get("page", 0),
            "items_found": run.get("items_found", 0),
            "items_inserted": run.get("items_inserted", 0),
            "items_updated": run.get("items_updated", 0),
            "items_skipped": run.get("items_skipped", 0),
            "errors": run.get("errors", []),
            "message": run.get("message", ""),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "duration_ms": run.get("duration_ms"),
            "api_hits": run.get("api_hits", 0),
            "pages_hit": run.get("pages_hit", []),
        }

    def _day_window(self, day: date | None = None) -> tuple[datetime, datetime]:
        """Aware datetimes covering a local day: [00:00, next 00:00).

        Defaults to today; pass an explicit ``day`` to build the window for
        another date (e.g. the day a run started).
        """
        day = day or timezone.localdate()
        day_start = timezone.make_aware(datetime.combine(day, datetime.min.time()))
        return day_start, day_start + timedelta(days=1)

    @staticmethod
    def _run_day(run: dict) -> date:
        """The local day a run belongs to — from its own started_at.

        Keying on the run's start time (not close time) means a scrape that
        crosses midnight still lands in the rollup of the day it STARTED.
        """
        started = run.get("started_at")
        if started:
            parsed = datetime.fromisoformat(started)
            if parsed.tzinfo is None:
                parsed = timezone.make_aware(parsed)
            return timezone.localtime(parsed).date()
        return timezone.localdate()

    def _close(self, run: dict, started: float) -> ScrapeLog:
        """Finalize a run and append it to the day-level logs (master + per-site).

        Returns the master ``ScrapeLog`` for the day (ONE row per day); the
        run's own stats live in the per-site log (see :meth:`last_run`).
        """
        finished = timezone.now()
        run["finished_at"] = finished.isoformat()
        run["duration_ms"] = int((time.monotonic() - started) * 1000)
        run["api_hits"] = len(self.api_calls)
        run.setdefault("errors", [])
        run.setdefault("message", "")

        # Source health timestamps.
        self.source.last_scraped_at = finished
        if run.get("status") == ScrapeStatus.SUCCESS:
            self.source.last_success_at = finished
        self.source.save(update_fields=["last_scraped_at", "last_success_at", "updated_at"])

        day = self._run_day(run)
        site_log_id = None
        try:
            # Per-website log first (hook) so the master can reference it.
            site_log_id = self.record_detail_log(run, day)
        except Exception:  # noqa: BLE001 - detail log must never break the run
            logger.exception("Failed to record detail log for %s", self.source.slug)
        try:
            return self._update_master_day_log(run, day, site_log_id)
        except Exception:  # noqa: BLE001 - day log must never break the run
            logger.exception("Failed to record master day log for %s", self.source.slug)
            return ScrapeLog.objects.filter(day=day).first()  # best effort

    def last_run(self, day: date | None = None) -> dict | None:
        """The most recent scrape run entry for this source.

        Delegates to the per-website day log (SITE_LOG_MODELS registry) so
        the master log stays lean while callers still get the run's stats
        (api_hits, items_*, duration, pages_hit, errors).
        """
        site_log = self._site_log_for_day(day or timezone.localdate())
        return site_log.last_run() if site_log is not None else None

    def _site_log_for_day(self, day: date, site_log_id: str | None = None):
        """The per-website log row for this source/day, if any.

        Tries the freshly-written site log first (by pk, returned by
        ``record_detail_log``); falls back to a (source, day) lookup so the
        master rollup still embeds the site data when the detail-log hook
        failed. Uses the ``SITE_LOG_MODELS`` registry so future website logs
        are picked up automatically.
        """
        for model in SITE_LOG_MODELS:
            if site_log_id:
                row = model.objects.filter(pk=site_log_id).first()
                if row is not None:
                    return row
            # Fresh pk missing (or not provided) — try this source's row.
            row = model.objects.filter(source=self.source, day=day).first()
            if row is not None:
                return row
        return None

    def _update_master_day_log(self, run: dict, day: date, site_log_id: str | None = None) -> ScrapeLog:
        """Append this run to the master ``ScrapeLog`` row for ``day``.

        ONE row per day: the day's overall totals plus a compact ``websites``
        JSON — a short bucket per website ({source, name, table, log_id,
        status, run_count, api_hits, items_*}). ``table`` + ``log_id`` point
        at the site's own log (e.g. AfriworkScrapeLog), where the full run
        detail (pages hit, http statuses, errors) lives. Failed runs are
        included because they did hit the API — their website's bucket shows
        a non-success ``status`` so the culprit stands out.
        """
        slug = self.source.slug

        master, _ = ScrapeLog.objects.get_or_create(day=day)
        master.run_count += 1
        master.api_hits += run.get("api_hits", 0)
        master.items_found += run.get("items_found", 0)
        master.items_inserted += run.get("items_inserted", 0)
        master.items_updated += run.get("items_updated", 0)
        master.items_skipped += run.get("items_skipped", 0)
        master.status = self._worst_status(master.status, run.get("status"))

        site_log = self._site_log_for_day(day, site_log_id)

        bucket = master.website(slug)
        if bucket is None:
            bucket = {
                "source": slug,
                "name": self.source.name,
                "table": site_log._meta.model.__name__ if site_log else None,
                "log_id": str(site_log.pk) if site_log else None,
                "status": site_log.status if site_log else run.get("status"),
                "run_count": 0,
                "api_hits": 0,
                "items_found": 0,
                "items_inserted": 0,
                "items_updated": 0,
                "items_skipped": 0,
            }
            master.websites.append(bucket)
        bucket["run_count"] += 1
        bucket["api_hits"] += run.get("api_hits", 0)
        bucket["items_found"] += run.get("items_found", 0)
        bucket["items_inserted"] += run.get("items_inserted", 0)
        bucket["items_updated"] += run.get("items_updated", 0)
        bucket["items_skipped"] += run.get("items_skipped", 0)
        if site_log is not None:
            bucket["table"] = site_log._meta.model.__name__
            bucket["log_id"] = str(site_log.pk)
            bucket["status"] = site_log.status

        master.save()
        return master

    def _warn_if_today_filter_inert(self) -> None:
        if self.only_today and not (self.source.pagination or {}).get("date_filter"):
            logger.warning(
                "Source %s has only_today enabled but no date_filter in its "
                "pagination rules — the today filter is a no-op",
                self.source.slug,
            )

    def _new_run(self, page: int = 0) -> dict:
        """A fresh run-state dict (JSON-safe timestamps)."""
        return {
            "status": ScrapeStatus.RUNNING,
            "page": page,
            "items_found": 0,
            "items_inserted": 0,
            "items_updated": 0,
            "items_skipped": 0,
            "errors": [],
            "message": "",
            "started_at": timezone.now().isoformat(),
            "finished_at": None,
            "duration_ms": None,
            "api_hits": 0,
            "pages_hit": [],
        }

    def scrape(self, page: int = 0) -> ScrapeLog:
        """Run the pipeline for a single page and append it to the day logs.

        Returns the master ``ScrapeLog`` for the day; the run's own stats are
        in ``self.last_run()``. Failing fast (e.g. bad HTTP status,
        unparseable payload) raises after the run has been persisted to the
        logs, so callers (Celery later) can retry.
        """
        self._warn_if_today_filter_inert()
        self.api_calls = []

        run = self._new_run(page)
        started = time.monotonic()
        try:
            result = self._run_page(page)
            run.update(
                {
                    "status": ScrapeStatus.PARTIAL if result["errors"] else ScrapeStatus.SUCCESS,
                    "items_found": result["found"],
                    "items_inserted": result["inserted"],
                    "items_updated": result["updated"],
                    "items_skipped": result["skipped"],
                    "errors": result["errors"],
                    "message": f"{len(result['errors'])} item(s) failed" if result["errors"] else "",
                    "pages_hit": [
                        {
                            "page": result["page"],
                            "http_status": result["http_status"],
                            "found": result["found"],
                        }
                    ],
                }
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the logs, then re-raised
            run["status"] = ScrapeStatus.FAILED
            run["message"] = str(exc)
            run["errors"] = [f"{type(exc).__name__}: {exc}"]
            # API usage must survive failures too (the page call still happened).
            run["pages_hit"] = [
                {"page": c["page"], "http_status": c.get("http_status")}
                for c in self.api_calls
            ]
            logger.exception("Scrape failed for source %s", self.source.slug)
            master = self._close(run, started)
            raise

        master = self._close(run, started)
        logger.info(
            "Scrape %s for %s: api_hits=%d found=%d inserted=%d updated=%d skipped=%d (%dms)",
            run["status"],
            self.source.slug,
            run["api_hits"],
            run["items_found"],
            run["items_inserted"],
            run["items_updated"],
            run["items_skipped"],
            run["duration_ms"],
        )
        return master

    def scrape_many(self, max_pages: int | None = None) -> ScrapeLog:
        """Sweep pages, appending ONE run entry to the day's master log.

        First scrape of the day (no records yet): pages are fetched until an
        empty page, so every listing of the day is captured. Re-scrapes: the
        sweep stops as soon as a page contains a record already stored for
        today (the last-known-record boundary), so only newly posted jobs are
        fetched. The sweep also stops on a page that repeats items already
        seen this run, or when max_pages is reached.

        Items are buffered, sorted chronologically (first posted = #01), then
        saved once. Every API request is tallied in ``api_hits``/``pages_hit``.
        Consequence: a mid-sweep failure persists no items (only the logs),
        which is safe because re-runs are idempotent via dedup.
        """
        self._warn_if_today_filter_inert()
        self.api_calls = []

        pagination = self.source.pagination or {}
        if max_pages is None:
            max_pages = int(pagination.get("max_pages", 50))

        # The incremental stop boundary: ids already stored for today. Only
        # used in today-only mode (a --no-today backfill must not stop early)
        # and only when the previous run completed cleanly (so the stored set
        # is a complete snapshot of the day). The stop itself relies on the
        # query's newest-first order_by: new jobs only ever appear above the
        # boundary, so once a page contains a known id, everything below it
        # is already stored. Computed BEFORE this run is logged so the safety
        # check sees the previous run, not our just-appended one.
        today_ids = (
            self._today_known_ids() if (self.only_today and self._incremental_stop_safe()) else set()
        )

        run = self._new_run(0)
        started = time.monotonic()

        totals = {"found": 0, "inserted": 0, "updated": 0, "skipped": 0, "errors": []}
        pages_hit: list[dict] = []
        seen_ids: set[str] = set()
        all_items: list[dict] = []
        try:
            for page in range(max_pages):
                items, http_status = self._page_items(page)
                totals["found"] += len(items)
                pages_hit.append(
                    {"page": page, "http_status": http_status, "found": len(items)}
                )

                page_ids = [str(i.get("external_id") or "") for i in items]
                all_seen_before = bool(page_ids) and all(
                    pid in seen_ids for pid in page_ids
                )
                seen_ids.update(page_ids)
                all_items.extend(items)

                if not items:
                    break
                if any(pid in today_ids for pid in page_ids):
                    # Reached the last known record — everything newer was new.
                    break
                if all_seen_before:
                    break
            else:
                totals["errors"].append(f"sweep truncated at max_pages={max_pages}")

            # Number chronologically across the whole sweep: the first job
            # posted that day is #01, even if pagination overlaps or returns
            # pages newest-first.
            all_items.sort(key=self._sort_key)
            inserted, updated, skipped, errors = self.save_items(all_items)
            totals["inserted"] = inserted
            totals["updated"] = updated
            totals["skipped"] = skipped
            totals["errors"] += errors

            run.update(
                {
                    "status": ScrapeStatus.PARTIAL if totals["errors"] else ScrapeStatus.SUCCESS,
                    "items_found": totals["found"],
                    "items_inserted": totals["inserted"],
                    "items_updated": totals["updated"],
                    "items_skipped": totals["skipped"],
                    "errors": totals["errors"],
                    "message": f"{len(totals['errors'])} item(s) failed" if totals["errors"] else "",
                    "pages_hit": pages_hit,
                }
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the logs, then re-raised
            run["status"] = ScrapeStatus.FAILED
            run["message"] = str(exc)
            run["errors"] = list(totals["errors"]) + [f"{type(exc).__name__}: {exc}"]
            run["items_found"] = totals["found"]
            run["items_inserted"] = totals["inserted"]
            run["items_updated"] = totals["updated"]
            run["items_skipped"] = totals["skipped"]
            run["pages_hit"] = pages_hit
            logger.exception("Sweep failed for source %s", self.source.slug)
            master = self._close(run, started)
            raise

        master = self._close(run, started)
        logger.info(
            "Sweep %s for %s: pages=%d api_hits=%d found=%d inserted=%d "
            "updated=%d skipped=%d (%dms)",
            run["status"],
            self.source.slug,
            len(pages_hit),
            run["api_hits"],
            run["items_found"],
            run["items_inserted"],
            run["items_updated"],
            run["items_skipped"],
            run["duration_ms"],
        )
        return master
