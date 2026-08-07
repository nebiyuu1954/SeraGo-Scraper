"""Compare a source's live API structure against its snapshot.

Usage:
    manage.py check_structure              # check every active source with a snapshot
    manage.py check_structure afriwork     # check one source

Fetches one page from the live API and diffs its field paths against the
stored snapshot. Any added/removed field is reported and the command exits
non-zero, so a silent API change becomes visible immediately.
"""
import httpx
from django.core.management.base import BaseCommand, CommandError

from core.models import Source
from core.scrapers import ScraperFactory
from core.structures import compare_structures, extract_structure, load_structure


class Command(BaseCommand):
    help = "Check whether a source's live API structure still matches its snapshot."

    def add_arguments(self, parser):
        parser.add_argument(
            "slug",
            nargs="?",
            default=None,
            help="Source slug; default: all active sources that have a snapshot.",
        )

    def handle(self, *args, **options):
        if options["slug"]:
            sources = Source.objects.filter(slug=options["slug"])
            if not sources.exists():
                raise CommandError(
                    f"No source with slug '{options['slug']}'. Run 'seed_sources' first."
                )
        else:
            sources = Source.objects.filter(is_active=True)

        checked = 0
        changed = 0
        skipped = 0
        for source in sources:
            stored = load_structure(source.slug)
            if stored is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"{source.slug}: no snapshot - run 'capture_structure {source.slug}' first."
                    )
                )
                continue

            checked += 1
            scraper = ScraperFactory.for_source(source)
            try:
                items = scraper.parse(scraper.fetch(0))
            except httpx.HTTPStatusError as exc:
                # Auth-required sources (e.g. EthioJobs JWT) can't be checked
                # when the token isn't configured — flag it and fail the run
                # so a cron'd daily_check knows the site was NOT verified.
                if exc.response.status_code in (401, 403):
                    skipped += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"{source.slug}: skipped - API auth not configured "
                            f"(HTTP {exc.response.status_code}). Set the token "
                            f"(e.g. ETHIOJOBS_TOKEN) and re-run."
                        )
                    )
                    continue
                raise
            if not items:
                self.stdout.write(
                    self.style.ERROR(f"{source.slug}: live API returned no items.")
                )
                changed += 1
                continue

            current = extract_structure(items[0])
            added, removed = compare_structures(current, stored.get("fields", []))
            if not added and not removed:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{source.slug}: structure OK ({len(current)} field(s))."
                    )
                )
                continue

            changed += 1
            self.stdout.write(self.style.ERROR(f"{source.slug}: STRUCTURE CHANGED"))
            for field in added:
                self.stdout.write(self.style.ERROR(f"  + added:   {field}"))
            for field in removed:
                self.stdout.write(self.style.ERROR(f"  - removed: {field}"))

        if changed:
            raise CommandError(
                f"{changed} of {checked} checked source(s) have a changed structure."
            )
        if skipped:
            raise CommandError(
                f"{skipped} of {checked} source(s) could NOT be verified "
                f"(API auth not configured). Fix the token and re-run."
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"All {checked} checked source(s) match their snapshots."
            )
        )
