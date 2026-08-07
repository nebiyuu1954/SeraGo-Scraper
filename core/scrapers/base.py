"""Base scraper machinery for SeraGo.

Pipeline: fetch -> parse -> normalize (field mapping) -> save, with a
ScrapeLog audit trail. Subclasses only implement :meth:`fetch` and
:meth:`parse`; everything else is configuration-driven through the
Source's ``field_mapping`` and ``pagination`` JSON.
"""
from __future__ import annotations

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable

from bs4 import BeautifulSoup
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import ScrapeLog, ScrapedItem, ScrapeStatus, Source

logger = logging.getLogger(__name__)


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

    # -- stages implemented by concrete scrapers --

    @abstractmethod
    def fetch(self, page: int = 0) -> Any:
        """Fetch one page of raw data. ``page`` is 0-based."""

    @abstractmethod
    def parse(self, raw: Any) -> list[dict]:
        """Extract a list of raw item dicts from the fetched payload."""

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

    def save_items(self, items: list[dict], log: ScrapeLog) -> None:
        """Upsert normalized items, deduplicating on (source, external_id).

        Counts inserted / updated / skipped on the log. Per-item failures are
        collected on ``log.errors`` so one bad item never kills the run.
        """
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
                self._upsert(external_id, content_hash, defaults, log)
            except Exception as exc:  # noqa: BLE001 - keep the run going
                log.errors.append(f"{item.get('external_id', '?')}: {exc}")

    def _upsert(
        self,
        external_id: str,
        content_hash: str,
        defaults: dict,
        log: ScrapeLog,
    ) -> None:
        """Insert or update one item, deduplicating on (source, external_id)."""
        existing = (
            ScrapedItem.objects.filter(source=self.source, external_id=external_id)
            .only("content_hash")
            .first()
        )
        if existing is None:
            try:
                # Own atomic block so the IntegrityError catch below stays safe
                # even if the caller already wrapped the pipeline in a transaction
                # (e.g. TestCase, Celery task wrapping scrape() in atomic).
                with transaction.atomic():
                    ScrapedItem.objects.create(
                        source=self.source, external_id=external_id, **defaults
                    )
            except IntegrityError:
                # Lost a race with a concurrent worker: reload and treat as existing.
                existing = (
                    ScrapedItem.objects.filter(source=self.source, external_id=external_id)
                    .only("content_hash")
                    .first()
                )
                if existing is None:
                    log.items_skipped += 1
                    return
            else:
                log.items_inserted += 1
                return

        if existing.content_hash == content_hash:
            # Seen again, unchanged: refresh last_seen_at, count as skipped.
            ScrapedItem.objects.filter(pk=existing.pk).update(last_seen_at=timezone.now())
            log.items_skipped += 1
        else:
            # Note: fields mapped to None are intentionally absent from
            # ``defaults`` so flaky source fields never wipe good data.
            for key, value in defaults.items():
                setattr(existing, key, value)
            existing.save(update_fields=list(defaults) + ["updated_at"])
            log.items_updated += 1

    def scrape(self, page: int = 0) -> ScrapeLog:
        """Run the full pipeline for one page and record a ScrapeLog.

        Failing fast (e.g. bad HTTP status, unparseable payload) raises after
        the log has been persisted, so callers (Celery later) can retry.
        """
        log = ScrapeLog.objects.create(
            source=self.source,
            status=ScrapeStatus.RUNNING,
            page=page,
        )
        started = time.monotonic()
        try:
            raw = self.fetch(page)
            items: list[dict] = []
            for raw_item in self.parse(raw):
                item = self.normalize(raw_item)
                item["raw_data"] = raw_item
                items.append(item)

            log.items_found = len(items)
            self.save_items(items, log)

            log.status = ScrapeStatus.PARTIAL if log.errors else ScrapeStatus.SUCCESS
            if log.errors:
                log.message = f"{len(log.errors)} item(s) failed"
            self.source.last_scraped_at = timezone.now()
            if log.status == ScrapeStatus.SUCCESS:
                self.source.last_success_at = timezone.now()
            self.source.save(update_fields=["last_scraped_at", "last_success_at", "updated_at"])
        except Exception as exc:  # noqa: BLE001 - recorded on the log, then re-raised
            log.status = ScrapeStatus.FAILED
            log.message = str(exc)
            if not log.errors:
                log.errors = [f"{type(exc).__name__}: {exc}"]
            logger.exception("Scrape failed for source %s", self.source.slug)
            raise
        finally:
            log.finished_at = timezone.now()
            log.duration_ms = int((time.monotonic() - started) * 1000)
            log.save()

        logger.info(
            "Scrape %s for %s: found=%d inserted=%d updated=%d skipped=%d (%dms)",
            log.status,
            self.source.slug,
            log.items_found,
            log.items_inserted,
            log.items_updated,
            log.items_skipped,
            log.duration_ms,
        )
        return log
