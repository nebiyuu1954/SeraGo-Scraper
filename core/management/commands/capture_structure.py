"""Snapshot a source's API response structure for change detection.

Usage:
    manage.py capture_structure <slug>

Fetches one page from the live API and writes the flattened field paths of
the first listing item to ``core/structure_snapshots/<slug>.json``.
"""
import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import Source
from core.scrapers import ScraperFactory
from core.structures import extract_structure, snapshot_path


class Command(BaseCommand):
    help = "Capture a source's API response structure into its snapshot file."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Source slug, e.g. 'afriwork'")

    def handle(self, *args, **options):
        try:
            source = Source.objects.get(slug=options["slug"])
        except Source.DoesNotExist as exc:
            raise CommandError(
                f"No source with slug '{options['slug']}'. Run 'seed_sources' first."
            ) from exc

        scraper = ScraperFactory.for_source(source)
        items = scraper.parse(scraper.fetch(0))
        if not items:
            raise CommandError(
                f"Source '{source.slug}' returned no items — nothing to snapshot."
            )

        payload = {
            "source": source.slug,
            "captured_at": timezone.now().isoformat(),
            "fields": extract_structure(items[0]),
        }
        path = snapshot_path(source.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Captured {len(payload['fields'])} field(s) for '{source.slug}' -> {path}"
            )
        )
