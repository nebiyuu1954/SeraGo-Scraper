"""One-time renumber (Option C): assign numbered_on/job_number from published_at.

For every ScrapedItem across all sources:
  - numbered_on  = the local day of published_at (kept as-is when published_at
                   is missing — we cannot know a better day).
  - job_number   = 1..N per (source, numbered_on), ordered by published_at
                   then external_id (mirrors the scraper's _sort_key: #01 is
                   the first job posted that day).

The per-site detail models (AfriworkJob, EthioJobsJob, ...) mirror the master
numbering, so they are synced afterwards.

Constraint safety: the unique (source, numbered_on, job_number) index is
cleared for the source first (job_number is nullable) and the renumber runs
inside one transaction, so no transient duplicate can ever be observed.
"""
import os
import sys
from pathlib import Path

# Make the project root importable (scripts/ lives one level down).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "serago.settings")

import django

django.setup()

from collections import defaultdict
from datetime import datetime, timezone as dt_timezone

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from core.models import (
    AfriworkJob,
    EthioJobsJob,
    GeezJob,
    HaHuJob,
    ReporterJob,
    ScrapedItem,
    Source,
)

DRY_RUN = "--dry-run" in sys.argv

# slug -> (detail model, ScrapedItem FK field name)
DETAIL_LINKS = {
    "afriwork": (AfriworkJob, "afriwork_job"),
    "ethiojobs": (EthioJobsJob, "ethiojobs_job"),
    "hahujobs": (HaHuJob, "hahujobs_job"),
    "geezjobs": (GeezJob, "geezjobs_job"),
    "reporterjobs": (ReporterJob, "reporter_job"),
}

_MIN = datetime.min.replace(tzinfo=dt_timezone.utc)


def local_day(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = timezone.make_aware(value)
    return timezone.localtime(value).date()


def sort_key(it):
    return (it.published_at is None, it.published_at or _MIN, it.external_id or "")


def plan_for_source(source):
    """Return (renumbered_rows, detail_rows) for one source without writing."""
    items = list(ScrapedItem.objects.filter(source=source).select_related())
    link = DETAIL_LINKS.get(source.slug)
    renumbered = []
    detail_rows = []
    groups = defaultdict(list)
    for it in items:
        day = local_day(it.published_at) or it.numbered_on
        groups[day].append(it)
    for day, group in groups.items():
        group.sort(key=sort_key)
        for number, it in enumerate(group, start=1):
            if (it.numbered_on, it.job_number) != (day, number):
                renumbered.append((it, day, number))
                if link:
                    detail = getattr(it, link[1], None)
                    if detail is not None and (detail.numbered_on, detail.job_number) != (day, number):
                        detail_rows.append((detail, day, number))
    return renumbered, detail_rows


def main():
    print("DRY RUN — no writes" if DRY_RUN else "RENUMBERING — writing to DB")
    total_masters = 0
    total_details = 0
    for source in Source.objects.filter(is_active=True).order_by("slug"):
        renumbered, detail_rows = plan_for_source(source)
        total_masters += len(renumbered)
        total_details += len(detail_rows)
        print(
            f"{source.slug}: {len(renumbered)} master rows, {len(detail_rows)} detail rows"
        )
        if DRY_RUN or not renumbered:
            continue

        with transaction.atomic():
            # Clear the (source, numbered_on, job_number) index so no
            # transient duplicate can appear while we rewrite the batch.
            ScrapedItem.objects.filter(source=source).update(job_number=None)
            masters = [(it, day, number) for it, day, number in renumbered]
            for it, day, number in masters:
                it.numbered_on = day
                it.job_number = number
            ScrapedItem.objects.bulk_update(
                [it for it, _, _ in masters], ["numbered_on", "job_number"], batch_size=500
            )
            if detail_rows:
                model = DETAIL_LINKS[source.slug][0]
                for detail, day, number in detail_rows:
                    detail.numbered_on = day
                    detail.job_number = number
                model.objects.bulk_update(
                    [d for d, _, _ in detail_rows], ["numbered_on", "job_number"], batch_size=500
                )
        print(f"  committed")

    if DRY_RUN:
        print(f"\nWould renumber {total_masters} master rows, sync {total_details} detail rows.")
        return

    print("\n--- verification ---")
    dupes = (
        ScrapedItem.objects.values("source", "numbered_on", "job_number")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .count()
    )
    print("duplicate (source, numbered_on, job_number) groups:", dupes)
    today = timezone.localdate()
    for source in Source.objects.filter(is_active=True).order_by("slug"):
        today_count = ScrapedItem.objects.filter(source=source, numbered_on=today).count()
        print(f"{source.slug}: numbered today = {today_count}")


if __name__ == "__main__":
    main()
