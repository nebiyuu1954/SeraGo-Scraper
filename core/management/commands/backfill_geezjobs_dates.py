"""Backfill published_at for GeezJobs items missing it.

Items scraped via Playwright before the SVG-icon fix (c2b9927) have
published_at=None because _chip_text() couldn't find <i data-lucide>
elements (Playwright replaces them with <svg class="lucide …">).

This command re-scrapes all pages with the fixed parser, which now handles
both raw and JS-rendered icons. The content_hash changes (company, location,
etc. are now populated from chip text), so the save pipeline updates the
existing rows with correct published_at.

Usage:
    python manage.py backfill_geezjobs_dates          # re-scrape all pages
    python manage.py backfill_geezjobs_dates --dry-run # preview without saving
"""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import GeezJob, ScrapedItem, Source

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Re-scrape GeezJobs to backfill published_at for items missing it."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be updated without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        source = Source.objects.get(slug="geezjobs")

        before_count = ScrapedItem.objects.filter(
            source=source, published_at__isnull=True
        ).count()
        self.stdout.write(
            f"GeezJobs items missing published_at: {before_count}"
        )

        if before_count == 0:
            self.stdout.write("Nothing to backfill.")
            return

        from core.scrapers.geezjobs import GeezJobsScraper

        scraper = GeezJobsScraper(source)

        # Re-scrape all pages (--no-today behavior: sweep until empty page).
        pagination = source.pagination or {}
        max_pages = int(pagination.get("max_pages", 20))
        self.stdout.write(f"Re-scraping up to {max_pages} pages…")

        if dry_run:
            # Quick check: fetch page 0 to verify the fix works, then
            # estimate the total from DB stats (no need to hit all pages).
            try:
                raw = scraper.fetch(0)
                items_raw = scraper.parse(raw)
                sample_updated = 0
                for raw_item in items_raw:
                    item = scraper.normalize(raw_item)
                    external_id = item.get("external_id")
                    if not external_id:
                        continue
                    existing = ScrapedItem.objects.filter(
                        source=source, external_id=external_id
                    ).first()
                    if existing and existing.published_at is None:
                        sample_updated += 1

                self.stdout.write(
                    f"  Page 0: {len(items_raw)} cards parsed, "
                    f"{sample_updated}/{len(items_raw)} would be updated."
                )
            except Exception as exc:
                self.stdout.write(self.style.WARNING(
                    f"  Page 0 fetch failed: {exc}"
                ))

            self.stdout.write(
                f"\n[dry-run] {before_count} items need published_at."
                f" Run without --dry-run to backfill."
            )
            return

        # Actually re-scrape. Disable today filter so ALL pages are swept,
        # and remove the time budget so the backfill can process all pages
        # (relay rotation through broken backends is slow locally but fast
        # on CI where Playwright works). The fixed _chip_text will parse
        # published_at correctly, and the content_hash will change
        # (company/location now populated from chip text), triggering the
        # update path on existing rows.
        scraper.only_today = False
        # Override max_source_seconds to unlimited for backfill
        pagination_copy = dict(source.pagination or {})
        pagination_copy["max_source_seconds"] = 999999
        source.pagination = pagination_copy
        log = scraper.scrape_many(max_pages=max_pages)

        after_count = ScrapedItem.objects.filter(
            source=source, published_at__isnull=True
        ).count()
        fixed = before_count - after_count

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Backfilled {fixed} items with published_at."
            f"\n  Before: {before_count} items without published_at"
            f"\n  After:  {after_count} items without published_at"
        ))

        if after_count > 0:
            self.stdout.write(self.style.WARNING(
                f"\n{after_count} items still have published_at=None."
                " These may be on pages that didn't match the today filter"
                " or had parsing issues."
            ))
