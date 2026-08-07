"""Manually trigger a scrape for one source.

Default: sweep pages until an empty page. The run's stats land in the
per-site day log; the master ScrapeLog keeps the day's overall totals.

Usage:
    manage.py scrape_source <slug>                 # sweep all pages (today only)
    manage.py scrape_source <slug> --page 2        # scrape a single page
    manage.py scrape_source <slug> --no-today      # ignore the today-only filter
"""
from django.core.management.base import BaseCommand, CommandError

from core.models import ScrapeStatus, Source
from core.scrapers import ScraperFactory


class Command(BaseCommand):
    help = "Scrape a source, sweeping pages until today's listings are covered."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Source slug, e.g. 'afriwork'")
        parser.add_argument(
            "--page",
            type=int,
            default=None,
            help="Scrape only this single 0-based page (debug). Default: sweep all pages.",
        )
        parser.add_argument(
            "--no-today",
            action="store_true",
            help="Disable the source's today-only filter for this run.",
        )

    def handle(self, *args, **options):
        try:
            source = Source.objects.get(slug=options["slug"])
        except Source.DoesNotExist as exc:
            raise CommandError(f"No source with slug '{options['slug']}'. Run 'seed_sources' first.") from exc

        scraper = ScraperFactory.for_source(source)
        if options["no_today"]:
            scraper.only_today = False

        if options["page"] is not None:
            master = scraper.scrape(page=options["page"])
        else:
            master = scraper.scrape_many()

        # The run's own stats live in the per-site day log (scraper.last_run);
        # the master row itself only aggregates the day's totals.
        run = scraper.last_run()
        if not run:
            self.stdout.write(self.style.WARNING(
                f"Run finished but no per-site stats were recorded; "
                f"day totals: {master.run_count} run(s), {master.api_hits} api_hits"
            ))
            return
        status = run.get("status", ScrapeStatus.SUCCESS)
        style = self.style.SUCCESS
        if status == ScrapeStatus.PARTIAL:
            style = self.style.WARNING
        elif status == ScrapeStatus.FAILED:
            style = self.style.ERROR
        self.stdout.write(style(
            f"[{status}] api_hits={run.get('api_hits', 0)} found={run.get('items_found', 0)} "
            f"inserted={run.get('items_inserted', 0)} updated={run.get('items_updated', 0)} "
            f"skipped={run.get('items_skipped', 0)} duration={run.get('duration_ms')}ms "
            f"(day: {master.run_count} run(s), {master.api_hits} api_hits)"
        ))
        for page in run.get("pages_hit", []):
            self.stdout.write(
                f"  page={page.get('page')} http={page.get('http_status')} found={page.get('found')}"
            )
        for error in run.get("errors", []):
            self.stdout.write(self.style.ERROR(f"  - {error}"))
