"""Archive ended jobs + the week's logs, then clean them out of the database.

The SeraGo job lifecycle keeps a listing in the scraper database while it is
visible (deadline + 7 days). After that window the listing has no product
value — the SeraGo side already hid it (and keeps only a tiny saved-job
snapshot for stats) — so this command moves the full record (all master +
per-site fields, including the raw payload) into a file, sends it to the
Telegram bot, and deletes the rows. Run weekly (Sunday) from the GitHub
Actions workflow.

Safety: nothing is deleted until BOTH files upload successfully. A failed
upload leaves every row in place and the next run retries — data is never
lost to a Telegram hiccup.

Files (sent to the bot as documents, retrievable from the chat forever):

* ``jobs-<date>.jsonl.gz`` — one line per ended listing: source, external_id,
  every master field, and the per-site detail (with ``raw_payload``). JSONL
  = one job per line: readable, grep-able, and loads straight into pandas.
* ``logs-<date>.json.gz`` — the week's master ``ScrapeLog`` + per-site day
  logs (the run history), so the log tables stay bounded too.

Usage:
    manage.py archive_week                  # build, send, delete
    manage.py archive_week --dry-run        # build + print, send/delete nothing
"""
import gzip
import io
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
from django.core.management.base import BaseCommand
from django.db.models import Model
from django.utils import timezone

from core.models import SITE_LOG_MODELS, ScrapeLog, ScrapedItem
from core.scrapers.base import LIFECYCLE_GRACE_DAYS

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"


def _json_safe(value):
    """Convert a model field value into a JSON-serializable form."""
    if value is None or isinstance(value, (str, int, bool, list, dict)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    return str(value)


def _field_dict(instance: Model) -> dict:
    """Every concrete field of a model instance, JSON-safe."""
    result = {}
    for field in instance._meta.concrete_fields:
        result[field.name] = _json_safe(getattr(instance, field.attname))
    return result


class Command(BaseCommand):
    help = (
        "Archive listings past their deadline+7-day lifecycle window plus the "
        "week's logs, send the files to Telegram, then delete the rows."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Build the files and print what would be archived/sent/deleted — no sends, no deletes.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing will be sent or deleted."))
        elif not token or not chat_id:
            self.stdout.write(
                self.style.WARNING(
                    "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — "
                    "building the files but NOT sending or deleting (check the workflow secrets)."
                )
            )
            dry_run = True

        now = timezone.now()
        cutoff = now - timedelta(days=LIFECYCLE_GRACE_DAYS)

        # ------------------------------------------------------------ jobs
        # Everything whose visibility window (deadline + 7 days) has ended.
        items = list(
            ScrapedItem.objects.filter(deadline__lt=cutoff)
            .select_related("source")
            .select_related("afriwork_job", "ethiojobs_job", "hahujobs_job", "geezjobs_job", "reporter_job")
        )
        jobs_lines: list[str] = []
        for item in items:
            detail = None
            for fk in ("afriwork_job", "ethiojobs_job", "hahujobs_job", "geezjobs_job", "reporter_job"):
                row = getattr(item, fk, None)
                if row is not None:
                    detail = {"model": type(row).__name__, "fields": _field_dict(row)}
                    break
            record = {
                "source": item.source.slug,
                "external_id": item.external_id,
                **_field_dict(item),
                "detail": detail,
            }
            jobs_lines.append(json.dumps(record, ensure_ascii=False))

        # ------------------------------------------------------------ logs
        # The week's run history: master ScrapeLog rows + per-site day logs.
        week_start = timezone.localdate() - timedelta(days=LIFECYCLE_GRACE_DAYS)
        master_logs = [
            _field_dict(log)
            for log in ScrapeLog.objects.filter(day__gte=week_start).order_by("day")
        ]
        website_logs = {}
        for model in SITE_LOG_MODELS:
            rows = list(model.objects.filter(day__gte=week_start).select_related("source").order_by("day"))
            if rows:
                website_logs[model.__name__] = [_field_dict(row) for row in rows]

        today = timezone.localdate()
        jobs_name = f"jobs-{today.isoformat()}.jsonl.gz"
        logs_name = f"logs-{today.isoformat()}.json.gz"
        jobs_gz = gzip.compress("\n".join(jobs_lines).encode("utf-8"))
        logs_payload = {
            "generated_at": now.isoformat(),
            "period": {"from": week_start.isoformat(), "to": today.isoformat()},
            "master": master_logs,
            "websites": website_logs,
        }
        logs_gz = gzip.compress(json.dumps(logs_payload, ensure_ascii=False, indent=2).encode("utf-8"))

        self.stdout.write(
            f"Archive run {today} — {len(items)} ended job(s), "
            f"master logs: {len(master_logs)} day(s), per-site logs: "
            f"{sum(len(v) for v in website_logs.values())} day-row(s)."
        )
        self.stdout.write(f"  {jobs_name}: {len(jobs_gz) / 1024:.1f} KB gzipped")
        self.stdout.write(f"  {logs_name}: {len(logs_gz) / 1024:.1f} KB gzipped")

        if not items and not master_logs and not website_logs:
            self.stdout.write(self.style.SUCCESS("Nothing to archive — done."))
            return

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"DRY RUN complete — would send 2 files and delete "
                    f"{len(items)} job row(s) + pruned logs."
                )
            )
            return

        # ------------------------------------------------------- send first
        self._send_document(token, chat_id, jobs_name, jobs_gz, f"{len(items)} ended jobs")
        self._send_document(
            token, chat_id, logs_name, logs_gz,
            f"Week's logs ({len(master_logs)} master day(s))",
        )
        self.stdout.write(self.style.SUCCESS("Both files uploaded to Telegram."))

        # --------------------------------------------------- delete on success
        archived_ids = [item.external_id for item in items]
        # Per-site detail rows first (ScrapedItem's OneToOne FKs are
        # SET_NULL, so the links clear themselves), then the master rows.
        detail_models = {
            row.__class__
            for row in (getattr(item, fk, None) for item in items for fk in
                        ("afriwork_job", "ethiojobs_job", "hahujobs_job", "geezjobs_job", "reporter_job"))
            if row is not None
        }
        deleted_details = 0
        for model in detail_models:
            deleted_details += model.objects.filter(external_id__in=archived_ids).delete()[0]
        deleted_items, _ = ScrapedItem.objects.filter(pk__in=[item.pk for item in items]).delete()
        # Prune logs older than the archived week (keeps ~1 week in the DB;
        # anything older was already archived by a previous Sunday run).
        pruned = 0
        for model in SITE_LOG_MODELS:
            pruned += model.objects.filter(day__lt=week_start).delete()[0]
        pruned += ScrapeLog.objects.filter(day__lt=week_start).delete()[0]

        self.stdout.write(
            self.style.SUCCESS(
                f"Archived + deleted: {deleted_items} job(s) (plus {deleted_details} "
                f"detail row(s)), pruned {pruned} log row(s) older than {week_start}."
            )
        )

    def _send_document(self, token: str, chat_id: str, filename: str, content: bytes, caption: str) -> None:
        """Upload one gzipped file to the Telegram bot chat (50 MB limit — we're at KBs)."""
        response = httpx.post(
            TELEGRAM_API.format(token=token) + "/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (filename, io.BytesIO(content), "application/gzip")},
            timeout=120.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description") or "Telegram API returned ok=false")
