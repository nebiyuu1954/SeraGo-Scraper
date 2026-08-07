"""Manually trigger a scrape for one source.

Usage: manage.py scrape_source <slug> [--page N]
"""
from django.core.management.base import BaseCommand, CommandError

from core.models import ScrapeStatus, Source
from core.scrapers import ScraperFactory


class Command(BaseCommand):
    help = "Scrape one page from a source identified by slug."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Source slug, e.g. 'afriwork'")
        parser.add_argument("--page", type=int, default=0, help="0-based page/offset index")

    def handle(self, *args, **options):
        try:
            source = Source.objects.get(slug=options["slug"])
        except Source.DoesNotExist as exc:
            raise CommandError(f"No source with slug '{options['slug']}'. Run 'seed_sources' first.") from exc

        scraper = ScraperFactory.for_source(source)
        try:
            log = scraper.scrape(page=options["page"])
        except Exception as exc:  # noqa: BLE001 - failure already recorded on ScrapeLog
            raise CommandError(f"Scrape failed: {exc}") from exc

        style = self.style.SUCCESS if log.status == ScrapeStatus.SUCCESS else self.style.WARNING
        self.stdout.write(style(
            f"[{log.status}] page={log.page} found={log.items_found} "
            f"inserted={log.items_inserted} updated={log.items_updated} "
            f"skipped={log.items_skipped} duration={log.duration_ms}ms"
        ))
        for error in log.errors:
            self.stdout.write(self.style.ERROR(f"  - {error}"))
