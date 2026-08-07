"""Wipe ALL scraped data while keeping the Source configuration.

Intended for production resets (e.g. before a clean re-scrape). Deletes
every data row — ScrapedItem, ScrapeLog, per-site detail rows and the
day-level rollups — but leaves the ``Source`` rows (endpoint, query, field
mapping, ...) untouched, so you can re-run ``scrape_source`` immediately.

Usage (production):
    python manage.py clear_data --yes

Safety rails:
  * Refuses to run when ``DEBUG=True`` (i.e. a dev DB) unless ``--force``.
  * Always asks for confirmation unless ``--yes`` is given.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import (
    AfriworkJob,
    AfriworkScrapeLog,
    EthioJobsJob,
    EthioJobsScrapeLog,
    GeezJob,
    GeezScrapeLog,
    HaHuJob,
    HaHuScrapeLog,
    ReporterJob,
    ReporterScrapeLog,
    ScrapeLog,
    ScrapedItem,
)

# All data tables that hold scraped content. Source is deliberately excluded.
DATA_MODELS = [
    ScrapedItem,
    ScrapeLog,
    AfriworkJob,
    AfriworkScrapeLog,
    EthioJobsJob,
    EthioJobsScrapeLog,
    HaHuJob,
    HaHuScrapeLog,
    GeezJob,
    GeezScrapeLog,
    ReporterJob,
    ReporterScrapeLog,
]


class Command(BaseCommand):
    help = (
        "Delete ALL scraped data (items, logs, per-site detail rows, day "
        "rollups) but keep the Source configuration."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Allow running even when DEBUG=True (i.e. a dev database).",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt.",
        )

    def handle(self, *args, **options):
        if settings.DEBUG and not options["force"]:
            raise CommandError(
                "Refusing to wipe data: DEBUG=True means this is probably your "
                "dev database. Pass --force to run anyway (production should "
                "run with DJANGO_DEBUG=false)."
            )

        counts = {m.__name__: m.objects.count() for m in DATA_MODELS}
        total = sum(counts.values())

        if total == 0:
            self.stdout.write(
                self.style.SUCCESS("Nothing to clear - all data tables are already empty.")
            )
            return

        self.stdout.write("Will delete:")
        for name, count in counts.items():
            if count:
                self.stdout.write(f"  {name}: {count} row(s)")
        self.stdout.write(f"Total: {total} row(s). Source config stays untouched.")

        if not options["yes"]:
            answer = input("Type 'yes' to confirm deletion: ").strip().lower()
            if answer != "yes":
                raise CommandError("Aborted — nothing was deleted.")

        deleted_total = 0
        for model in DATA_MODELS:
            deleted, _ = model.objects.all().delete()
            deleted_total += deleted

        self.stdout.write(
            self.style.SUCCESS(
                f"Cleared {deleted_total} row(s). Sources are still configured - "
                "run 'python manage.py scrape_source afriwork' to re-scrape."
            )
        )
