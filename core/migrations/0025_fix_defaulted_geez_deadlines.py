"""Backfill real GeezJobs deadlines for rows the 0022 backfill defaulted.

Migration 0022 gave every listing without a deadline ``published + 30 days``
and flagged it (``deadline_is_default``). For GeezJobs that flag was wrong in
~243 cases: those rows predate the deadline_text -> column mapping, so their
deadline was defaulted even though the ORIGINAL card text (kept in the raw
payload) contains a real deadline like "Deadline: August 14, 2026". This
migration re-derives the real deadline from ``raw_payload.deadline_text``,
writes it to both the master ``ScrapedItem`` and the ``GeezJob`` row, and
clears the flag — so the daily report stops nagging about a source that was
parsing fine all along.

Idempotent: only touches rows still flagged, and only when the text parses.
"""
import re
from datetime import datetime

from django.db import migrations
from django.utils import timezone

_MONTHS = {
    name: index
    for index, name in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}
# "Deadline: August 14, 2026" -> ("August", 14, 2026). The shared HTML parser
# this mirrors is stricter, but a plain regex is all a one-time backfill needs.
_DEADLINE_RE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})")


def _parse_deadline_text(text: str) -> datetime | None:
    match = _DEADLINE_RE.search(text or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if not month:
        return None
    try:
        return timezone.make_aware(
            datetime(int(match.group(3)), month, int(match.group(2)))
        )
    except ValueError:
        return None


def fix_defaulted_geez_deadlines(apps, schema_editor):
    ScrapedItem = apps.get_model("core", "ScrapedItem")
    GeezJob = apps.get_model("core", "GeezJob")

    items = (
        ScrapedItem.objects.filter(deadline_is_default=True, geezjobs_job__isnull=False)
        .select_related("geezjobs_job")
        .iterator()
    )
    fixed = 0
    for item in items:
        raw = item.geezjobs_job.raw_payload or {}
        text = raw.get("deadline_text") or item.geezjobs_job.deadline_text or ""
        deadline = _parse_deadline_text(text)
        if deadline is None:
            continue
        item.deadline = deadline
        item.deadline_is_default = False
        item.save(update_fields=["deadline", "deadline_is_default"])
        GeezJob.objects.filter(pk=item.geezjobs_job_id).update(
            deadline=deadline, deadline_text=text
        )
        fixed += 1

    if fixed:
        print(f"  Backfilled {fixed} real GeezJobs deadline(s) from raw card text.")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0024_scrapestat"),
    ]

    operations = [
        migrations.RunPython(fix_defaulted_geez_deadlines, noop),
    ]
