"""Archive ended jobs + the week's logs, then clean them out of the database.

The SeraGo job lifecycle keeps a listing in the scraper database while it is
visible (deadline + 7 days). After that window the listing has no product
value — the SeraGo side already hid it (and keeps only a tiny saved-job
snapshot for stats) — so this command moves the full record (master fields +
site-unique fields, including the raw payload) into a file, sends it to the
Telegram bot, and deletes the rows.

The flow (one calendar week, Mon–Sun):

* ``--step sunday`` (default, runs Sunday ~23:45 Addis, after the last
  scrape): files EVERYTHING still in the database — every job past its
  deadline+7 window and every log row — into two gzipped files, sends them
  to the bot, and on success deletes the jobs and the Mon–Sat log rows.
  **Sunday's own log rows are kept** so you can inspect the day in the
  database on Monday. A successful send also writes an ``ArchiveRun``
  row — the "sent" note.
* ``--step monday`` (runs Monday 18:00 Addis): if the "sent" note for the
  Sunday just past exists, the Sunday archive succeeded and this step
  simply deletes Sunday's kept log rows — the database is then empty and
  the new week starts fresh. If the note is missing, the Sunday archive
  failed: this step RETRIES it (filing everything still in the database,
  possibly two weeks after a longer outage), sends, and on success deletes
  everything — you already had Monday daytime to inspect.

Safety: nothing is deleted until BOTH files upload successfully. A failed
upload leaves every row in place, sends you a Telegram warning, and the
step exits non-zero so the workflow shows red. The Monday step only ever
deletes data that a successful send covered — the ``ArchiveRun`` note
guarantees a failed Sunday's data can never be deleted un-filed. There is
no nightly retry: if the Monday retry fails too, you fix the problem and
rerun the workflow manually (Actions → "SeraGo weekly archive" → Run
workflow).

Files (sent to the bot as documents, retrievable from the chat forever):

* ``jobs-<sunday>.jsonl.gz`` — one line per ended listing, with NO
  duplicated data: the master record (source, external_id, title,
  description, deadline, ...) plus the site-unique fields
  (experience_level, sectors, salary, application_method, ...) promoted
  flat to the top level. The per-site rows re-store the master fields
  (title, description, ...), so those copies are dropped; only what the
  master doesn't already have is kept. The untouched original API/HTML
  payload lives once under ``detail.raw_payload`` — the audit copy. JSONL
  = one job per line: readable, grep-able, and loads straight into pandas.
* ``logs-<sunday>.json.gz`` — the week's master ``ScrapeLog`` rows (day
  totals + sweep summaries, minus the redundant per-site buckets) and the
  per-site day logs (the full run history), so the log tables stay bounded
  too.

Schema markers: every file carries a ``schema`` field (``"jobs.v2"`` /
``"logs.v2"``) so a file's format is always self-describing — Telegram
keeps files forever, and the format will evolve. v1 (unmarked) was the
original verbose format with the duplicated ``detail.fields`` layer and the
master ``websites`` buckets; v2 is the deduplicated one.

Usage:
    manage.py archive_week                    # Sunday: file, send, keep Sunday
    manage.py archive_week --step monday      # Monday: clear Sunday, or retry
    manage.py archive_week --dry-run          # build + print, send/delete nothing
    manage.py archive_week --out-dir DIR      # write files locally, no send/delete
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
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Model
from django.utils import timezone

from core.models import SITE_LOG_MODELS, ArchiveRun, ScrapeLog, ScrapedItem
from core.scrapers.base import LIFECYCLE_GRACE_DAYS

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"

# The per-site detail rows (OneToOne from ScrapedItem) in definition order.
DETAIL_FKS = ("afriwork_job", "ethiojobs_job", "hahujobs_job", "geezjobs_job", "reporter_job")

# Per-site fields that reuse a MASTER field name but hold different data —
# renamed so nothing is lost and nothing collides with the master record.
DETAIL_RENAMES = {
    "GeezJob": {"job_type": "job_type_filter"},  # permanent/contract/... vs the master's full_time/part_time
    "HaHuJob": {"source": "aggregator_source"},  # upstream aggregator site vs the master source slug
}

# Per-site fields that are just the master field under another name (same
# value, different column) — the master copy wins, the site's copy is dropped.
DETAIL_DUPLICATES = {
    "entity_name": "company",  # Afriwork/HaHu call the posting company entity_name
}


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
    """Every concrete field of a model instance, JSON-safe.

    Relation fields (FK/OneToOne id columns) are skipped — callers add
    readable keys like ``source`` (the slug) themselves.
    """
    result = {}
    for field in instance._meta.concrete_fields:
        if field.is_relation:
            continue
        result[field.name] = _json_safe(getattr(instance, field.attname))
    return result


def _is_empty(value) -> bool:
    """Empty values carry no information and are dropped from the archive."""
    return value is None or value == "" or value == [] or value == {}


def _build_job_record(item) -> dict:
    """One canonical, non-redundant record per listing.

    Master fields first (the normalized contract). Then the site-unique
    fields are promoted flat — but a per-site field is kept only when the
    master doesn't already have it (per-site rows re-store title,
    description, deadline, ...), and empty values are dropped. The untouched
    original payload is the single ``detail.raw_payload`` audit copy.
    """
    record = {"schema": "jobs.v2"}
    record.update({k: v for k, v in _field_dict(item).items() if not _is_empty(v)})
    record["source"] = item.source.slug
    record["source_name"] = item.source.name

    for fk in DETAIL_FKS:
        row = getattr(item, fk, None)
        if row is None:
            continue
        model_name = type(row).__name__
        renames = DETAIL_RENAMES.get(model_name, {})
        for field in row._meta.concrete_fields:
            if field.is_relation:
                continue
            name = field.name
            if name in ("raw_payload", "created_at", "updated_at"):
                continue
            # Duplicate alias first (entity_name -> company — collides with the
            # master and is dropped), then the site-specific rename (Geez
            # job_type -> job_type_filter, HaHu source -> aggregator_source —
            # avoids the master collision and is kept). Both BEFORE the
            # collision check so renamed fields are never dropped for
            # colliding with their old name.
            name = DETAIL_DUPLICATES.get(name, name)
            name = renames.get(name, name)
            if name in record:
                continue
            value = _json_safe(getattr(row, field.attname))
            if not _is_empty(value):
                record[name] = value
        record["detail"] = {"model": model_name, "raw_payload": row.raw_payload or {}}
        break

    return record


def _collect(now: datetime):
    """Everything still in the database that the archive must file.

    Jobs: every listing past its deadline + grace window. Logs: EVERY log
    row still present (normally exactly the calendar week Mon–Sun; after a
    failed Sunday it is two weeks — filing everything is what makes a failed
    week self-healing instead of lost).
    """
    cutoff = now - timedelta(days=LIFECYCLE_GRACE_DAYS)
    items = list(
        ScrapedItem.objects.filter(deadline__lt=cutoff)
        .select_related("source")
        .select_related("afriwork_job", "ethiojobs_job", "hahujobs_job", "geezjobs_job", "reporter_job")
    )
    # Stable ordering (source, then deadline) so the file is easy to scan
    # and identical runs produce identical output.
    items.sort(key=lambda it: (it.source.slug, str(it.deadline or "")))

    master_logs = []
    for log in ScrapeLog.objects.order_by("day"):
        row = _field_dict(log)
        # The master's `websites` bucket (per-site summary + log_id links)
        # is fully duplicated by the per-site rows in the `websites` section
        # below — same status/run_count/api_hits/items_*, plus the full
        # scraped_log there — so it is dropped from the archive.
        row.pop("websites", None)
        master_logs.append(row)

    website_logs = {}
    for model in SITE_LOG_MODELS:
        rows = list(model.objects.select_related("source").order_by("day"))
        if rows:
            website_logs[model.__name__] = [
                {**_field_dict(row), "source": row.source.slug} for row in rows
            ]
    return items, master_logs, website_logs


def _build_files(now: datetime, archived_on: date, items, master_logs, website_logs):
    """Build the two gzipped files, named after the Sunday they cover."""
    jobs_lines = [json.dumps(_build_job_record(item), ensure_ascii=False) for item in items]
    jobs_name = f"jobs-{archived_on.isoformat()}.jsonl.gz"
    logs_name = f"logs-{archived_on.isoformat()}.json.gz"
    jobs_gz = gzip.compress("\n".join(jobs_lines).encode("utf-8"))
    days = [row["day"] for row in master_logs]
    period_from = min(days) if days else archived_on.isoformat()
    logs_payload = {
        "schema": "logs.v2",
        "generated_at": now.isoformat(),
        "period": {"from": period_from, "to": archived_on.isoformat()},
        "master": master_logs,
        "websites": website_logs,
    }
    logs_gz = gzip.compress(json.dumps(logs_payload, ensure_ascii=False, indent=2).encode("utf-8"))
    return jobs_name, jobs_gz, logs_name, logs_gz


def _delete_archived_jobs(items):
    """Delete the archived per-site detail rows then the master rows."""
    archived_ids = [item.external_id for item in items]
    detail_models = {
        row.__class__
        for row in (getattr(item, fk, None) for item in items for fk in DETAIL_FKS)
        if row is not None
    }
    deleted_details = 0
    for model in detail_models:
        deleted_details += model.objects.filter(external_id__in=archived_ids).delete()[0]
    deleted_items, _ = ScrapedItem.objects.filter(pk__in=[item.pk for item in items]).delete()
    return deleted_items, deleted_details


def _delete_logs_before(day: date) -> int:
    """Delete every log row from before ``day`` (day itself is kept)."""
    pruned = 0
    for model in SITE_LOG_MODELS:
        pruned += model.objects.filter(day__lt=day).delete()[0]
    pruned += ScrapeLog.objects.filter(day__lt=day).delete()[0]
    return pruned


def _delete_all_logs() -> int:
    pruned = 0
    for model in SITE_LOG_MODELS:
        pruned += model.objects.all().delete()[0]
    pruned += ScrapeLog.objects.all().delete()[0]
    return pruned


def _send_message(token: str, chat_id: str, text: str) -> None:
    """Send a plain-text message to the bot chat (failure warnings)."""
    response = httpx.post(
        TELEGRAM_API.format(token=token) + "/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=60.0,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description") or "Telegram API returned ok=false")


class Command(BaseCommand):
    help = (
        "Sunday: file everything past its lifecycle window + the week's logs, "
        "send to Telegram, delete Mon–Sat (keep Sunday). Monday: clear "
        "Sunday's kept data — or retry the archive if Sunday failed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--step",
            choices=["sunday", "monday"],
            default="sunday",
            help="sunday = file + send + keep Sunday (default); monday = clear Sunday or retry.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Build the files and print what would happen — no sends, no deletes.",
        )
        parser.add_argument(
            "--out-dir",
            default=None,
            help="Write the two gzipped files to this directory and stop — no send, no delete. "
            "For inspecting the archive format before it goes to Telegram.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"]) or bool(options["out_dir"])
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
        today = timezone.localdate()
        if options["step"] == "monday":
            self._step_monday(now, today, token, chat_id, dry_run, options)
        else:
            self._step_sunday(now, today, token, chat_id, dry_run, options)

    # ------------------------------------------------------------- Sunday
    def _step_sunday(self, now, today, token, chat_id, dry_run, options):
        self.stdout.write(f"SUNDAY archive — week ending {today}")
        items, master_logs, website_logs = _collect(now)
        jobs_name, jobs_gz, logs_name, logs_gz = _build_files(now, today, items, master_logs, website_logs)
        self._print_summary(items, master_logs, website_logs, jobs_name, jobs_gz, logs_name, logs_gz)

        if not items and not master_logs and not website_logs:
            self.stdout.write(self.style.SUCCESS("Nothing to archive — done."))
            return

        if options["out_dir"]:
            self._write_local(options["out_dir"], jobs_name, jobs_gz, logs_name, logs_gz)
            return
        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    "DRY RUN — would send both files, delete the archived jobs, "
                    "delete Mon–Sat log rows and KEEP Sunday's for your Monday look."
                )
            )
            return

        # --------------------------------------------------- send first
        try:
            self._send_document(token, chat_id, jobs_name, jobs_gz, f"{len(items)} ended jobs")
            self._send_document(
                token, chat_id, logs_name, logs_gz,
                f"Week's logs ({len(master_logs)} master day(s))",
            )
        except Exception as exc:  # noqa: BLE001 — any failure = notify + keep everything
            self._fail(
                token, chat_id,
                f"⚠️ SUNDAY ARCHIVE FAILED — {type(exc).__name__}: {exc}\n"
                "Nothing was deleted; all data is kept safely in the database.\n"
                "It will retry Monday at 18:00.",
            )
        self.stdout.write(self.style.SUCCESS("Both files uploaded to Telegram."))

        # ------------------------------------------- delete on success
        ArchiveRun.objects.update_or_create(
            archived_on=today,
            defaults={
                "jobs_file": jobs_name,
                "logs_file": logs_name,
                "jobs_count": len(items),
                "log_rows": len(master_logs) + sum(len(v) for v in website_logs.values()),
            },
        )
        deleted_items, deleted_details = _delete_archived_jobs(items)
        pruned = _delete_logs_before(today)  # Mon–Sat; Sunday's rows are kept
        self.stdout.write(
            self.style.SUCCESS(
                f"Archived + deleted {deleted_items} job(s) (plus {deleted_details} detail "
                f"row(s)); deleted {pruned} log row(s) from before {today}. "
                f"Sunday's logs are kept for your Monday look."
            )
        )

    # ------------------------------------------------------------- Monday
    def _step_monday(self, now, today, token, chat_id, dry_run, options):
        last_sunday = today - timedelta(days=1)
        self.stdout.write(f"MONDAY cleanup — {last_sunday}'s kept data")
        note = ArchiveRun.objects.filter(archived_on=last_sunday).first()

        if note is not None:
            # The Sunday file was sent — Sunday's kept log rows are filed and
            # safe to clear (you had Monday daytime to inspect them).
            if options["out_dir"]:
                self.stdout.write("Nothing to build — the Sunday archive already succeeded.")
                return
            if dry_run:
                self.stdout.write(self.style.SUCCESS("DRY RUN — would delete Sunday's kept log rows."))
                return
            pruned = _delete_logs_before(today)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Sunday's kept log rows cleared ({pruned} row(s)). "
                    "Database is empty — the new week starts fresh."
                )
            )
            return

        # No "sent" note for last Sunday → the Sunday archive failed (or was
        # never run): retry it now, filing everything still in the database.
        self.stdout.write(
            self.style.WARNING(
                "No 'sent' note for last Sunday — the Sunday archive failed. Retrying now."
            )
        )
        items, master_logs, website_logs = _collect(now)
        jobs_name, jobs_gz, logs_name, logs_gz = _build_files(now, last_sunday, items, master_logs, website_logs)
        self._print_summary(items, master_logs, website_logs, jobs_name, jobs_gz, logs_name, logs_gz)

        if not items and not master_logs and not website_logs:
            self.stdout.write(self.style.SUCCESS("Nothing pending to archive — done."))
            return

        if options["out_dir"]:
            self._write_local(options["out_dir"], jobs_name, jobs_gz, logs_name, logs_gz)
            return
        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    "DRY RUN — would retry the archive: send both files, then delete "
                    "everything (you already had Monday daytime to inspect the data)."
                )
            )
            return

        try:
            self._send_document(token, chat_id, jobs_name, jobs_gz, f"{len(items)} ended jobs (retry)")
            self._send_document(
                token, chat_id, logs_name, logs_gz,
                f"Week's logs ({len(master_logs)} master day(s)) (retry)",
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(
                token, chat_id,
                f"⚠️ MONDAY ARCHIVE RETRY FAILED — {type(exc).__name__}: {exc}\n"
                "Nothing was deleted; all data is kept safely in the database.\n"
                "Fix the problem, then rerun: Actions → 'SeraGo weekly archive' → Run workflow.",
            )
        self.stdout.write(self.style.SUCCESS("Retry uploaded both files to Telegram."))

        ArchiveRun.objects.update_or_create(
            archived_on=last_sunday,
            defaults={
                "jobs_file": jobs_name,
                "logs_file": logs_name,
                "jobs_count": len(items),
                "log_rows": len(master_logs) + sum(len(v) for v in website_logs.values()),
            },
        )
        deleted_items, deleted_details = _delete_archived_jobs(items)
        pruned = _delete_all_logs()  # week is over; you already inspected it
        self.stdout.write(
            self.style.SUCCESS(
                f"Retry succeeded: archived + deleted {deleted_items} job(s) (plus "
                f"{deleted_details} detail row(s)) and {pruned} log row(s). "
                "Database is empty — the new week starts fresh."
            )
        )

    # ------------------------------------------------------------ helpers
    def _print_summary(self, items, master_logs, website_logs, jobs_name, jobs_gz, logs_name, logs_gz):
        self.stdout.write(
            f"  {len(items)} ended job(s), "
            f"master logs: {len(master_logs)} day(s), per-site logs: "
            f"{sum(len(v) for v in website_logs.values())} day-row(s)."
        )
        self.stdout.write(f"  {jobs_name}: {len(jobs_gz) / 1024:.1f} KB gzipped")
        self.stdout.write(f"  {logs_name}: {len(logs_gz) / 1024:.1f} KB gzipped")

    def _write_local(self, out_dir, jobs_name, jobs_gz, logs_name, logs_gz):
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, jobs_name), "wb") as f:
            f.write(jobs_gz)
        with open(os.path.join(out_dir, logs_name), "wb") as f:
            f.write(logs_gz)
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {jobs_name} and {logs_name} to {out_dir} — "
                "nothing sent, nothing deleted."
            )
        )

    def _fail(self, token, chat_id, text):
        """Notify the user, print the error, and exit non-zero (workflow red)."""
        try:
            if token and chat_id:
                _send_message(token, chat_id, text)
        except Exception as exc:  # noqa: BLE001 — the notify failing must not mask the real error
            self.stdout.write(self.style.ERROR(f"Could not send failure notification: {exc}"))
        self.stdout.write(self.style.ERROR(text))
        raise CommandError(text.splitlines()[0])

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
