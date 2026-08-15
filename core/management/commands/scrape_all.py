"""Scrape every active source with one command.

Runs ``scrape_source <slug>`` for each active source, one after another — so
each website's run flows through the exact same pipeline and writes to its
own per-site day log + the master ``ScrapeLog`` exactly as if you had called
the individual command. One source failing never stops the others; the exit
code is non-zero when any source failed (handy for cron).

Usage:
    manage.py scrape_all                     # scrape every active source
    manage.py scrape_all --no-today          # ignore today-only filters
    manage.py scrape_all --slugs afriwork ethiojobs
    manage.py scrape_all --page 2            # single-page debug run per source
"""
import logging
from datetime import datetime

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import ScrapeLog, ScrapeStatus, Source
from core.scrapers import ScraperFactory

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Scrape every active source, one after another (each site logs independently)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-today",
            action="store_true",
            help="Disable the today-only filter for every source in this run.",
        )
        parser.add_argument(
            "--slugs",
            nargs="*",
            default=None,
            help="Only scrape these source slugs (default: all active sources).",
        )
        parser.add_argument(
            "--page",
            type=int,
            default=None,
            help="Scrape only this single 0-based page of every source (debug).",
        )

    def handle(self, *args, **options):
        sources = Source.objects.filter(is_active=True)
        requested = options["slugs"]
        if requested:
            known = set(
                Source.objects.filter(slug__in=requested).values_list("slug", flat=True)
            )
            missing = [slug for slug in requested if slug not in known]
            if missing:
                raise CommandError(
                    f"No source(s) with slug(s): {', '.join(missing)}. "
                    "Run 'seed_sources' first or check the slug."
                )
            inactive = sorted(
                set(requested)
                - set(
                    Source.objects.filter(
                        is_active=True, slug__in=requested
                    ).values_list("slug", flat=True)
                )
            )
            for slug in inactive:
                self.stdout.write(
                    self.style.WARNING(f"  {slug}: exists but is inactive — skipped.")
                )
            sources = sources.filter(slug__in=requested)
        sources = list(sources.order_by("slug"))
        if not sources:
            self.stdout.write(
                self.style.WARNING(
                    "No active sources to scrape. Run 'seed_sources' first."
                )
            )
            return

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Scraping {len(sources)} source(s): "
                + ", ".join(s.slug for s in sources)
            )
        )

        results: dict[str, str] = {}
        for index, source in enumerate(sources, start=1):
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    f"== {index}/{len(sources)} {source.slug} ({source.name}) =="
                )
            )
            kwargs = {}
            if options["no_today"]:
                kwargs["no_today"] = True
            if options["page"] is not None:
                kwargs["page"] = options["page"]
            try:
                # Exactly the individual command: same pipeline, same logs.
                call_command("scrape_source", source.slug, **kwargs)
                results[source.slug] = "success"
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
                results[source.slug] = f"failed: {exc}"
                logger.exception("Scrape failed for source %s", source.slug)
                self.stdout.write(
                    self.style.ERROR(f"  {source.slug} failed: {exc}")
                )

        day = timezone.localdate()
        master = ScrapeLog.objects.filter(day=day).first()
        if master is None:
            # Runs log under the day they STARTED, so a sweep that crossed
            # midnight has its logs on yesterday's master row — fall back to
            # the newest master rather than claiming nothing was recorded.
            master = ScrapeLog.objects.order_by("-day").first()
            if master is not None:
                day = master.day

        # One short entry per overall sweep: what happened this run (times +
        # totals + status). Full per-site detail stays in each site's day log.
        self._record_sweep_run(master, sources, results, day)

        self.stdout.write(self.style.MIGRATE_HEADING("== Summary =="))
        for slug, outcome in results.items():
            style = self.style.SUCCESS if outcome == "success" else self.style.ERROR
            self.stdout.write(style(f"  {slug}: {outcome}"))
        if master is not None:
            self.stdout.write(
                f"Day {day}: {master.run_count} run(s), {master.api_hits} api_hits, "
                f"{master.websites_count} website(s), status={master.status}, "
                f"items found={master.items_found} inserted={master.items_inserted} "
                f"updated={master.items_updated} skipped={master.items_skipped}"
            )
            if master.runs:
                sweep_times = ", ".join(r["time"] for r in master.runs)
                self.stdout.write(f"  Sweeps: {len(master.runs)} ({sweep_times} Addis)")
        else:
            self.stdout.write(self.style.WARNING(f"Day {day}: no master log yet."))

        failed = [slug for slug, outcome in results.items() if outcome != "success"]
        if failed:
            self.stdout.write(
                self.style.ERROR(f"{len(failed)} source(s) failed: {', '.join(failed)}")
            )
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("All sources scraped successfully."))

    # ------------------------------------------------------------------
    # Per-sweep summary (the master's short ``runs`` list)
    # ------------------------------------------------------------------

    _STATUS_WEIGHT = {
        ScrapeStatus.RUNNING: 0,
        ScrapeStatus.SUCCESS: 1,
        ScrapeStatus.PARTIAL: 2,
        ScrapeStatus.FAILED: 3,
    }

    def _worst_status(self, *statuses: str | None) -> str:
        """The most severe status (failed > partial > success > running)."""
        return max(
            statuses,
            key=lambda status: self._STATUS_WEIGHT.get(status, 0),
            default=ScrapeStatus.SUCCESS,
        )

    @staticmethod
    def _outcome_status(outcome: str) -> str:
        """Map a sweep outcome string to a status ('success' -> success)."""
        return ScrapeStatus.FAILED if outcome != "success" else ScrapeStatus.SUCCESS

    @staticmethod
    def _local_time_label(started_at: str | None) -> str:
        """HH:MM (Addis Ababa local time) of the sweep start, e.g. '12:00'."""
        if started_at:
            parsed = datetime.fromisoformat(started_at)
            if parsed.tzinfo is None:
                parsed = timezone.make_aware(parsed)
            return timezone.localtime(parsed).strftime("%H:%M")
        return timezone.localtime().strftime("%H:%M")

    def _record_sweep_run(
        self,
        master: ScrapeLog | None,
        sources: list[Source],
        results: dict[str, str],
        day,
    ) -> None:
        """Append one short entry to the master's ``runs`` for this sweep.

        One entry per overall scrape run (the scheduled 2x-daily sweep):
        ``{run, time, hits, found, inserted, updated, skipped, status}``.
        Totals are summed from each site's just-written run (per-site day
        log); when a site produced no run entry (e.g. it crashed before
        logging), the outcome recorded by the sweep loop decides its status.
        Full per-site detail stays in each website's own day log — this is
        only the short overall summary.
        """
        try:
            total_hits = total_found = total_inserted = total_updated = total_skipped = 0
            statuses: list[str] = []
            started_at: str | None = None
            for source in sources:
                outcome = results.get(source.slug, "failed")
                site_run: dict | None = None
                try:
                    site_run = ScraperFactory.for_source(source).last_run(day)
                except Exception:  # noqa: BLE001 - logging must never break the sweep
                    logger.exception("Could not read site log for %s", source.slug)
                if site_run is not None:
                    total_hits += site_run.get("api_hits", 0)
                    total_found += site_run.get("items_found", 0)
                    total_inserted += site_run.get("items_inserted", 0)
                    total_updated += site_run.get("items_updated", 0)
                    total_skipped += site_run.get("items_skipped", 0)
                    statuses.append(site_run.get("status") or self._outcome_status(outcome))
                    ts = site_run.get("started_at")
                    if ts and (started_at is None or ts < started_at):
                        started_at = ts
                else:
                    statuses.append(self._outcome_status(outcome))
            if not statuses:
                return
            if master is None:
                # No site logged (all mocked, or every run crashed before
                # logging) — the sweep summary still deserves a master row.
                master, _ = ScrapeLog.objects.get_or_create(day=day)
            master.runs.append(
                {
                    "run": len(master.runs) + 1,
                    "time": self._local_time_label(started_at),
                    "hits": total_hits,
                    "found": total_found,
                    "inserted": total_inserted,
                    "updated": total_updated,
                    "skipped": total_skipped,
                    "status": self._worst_status(*statuses),
                }
            )
            master.save(update_fields=["runs", "updated_at"])
        except Exception:  # noqa: BLE001 - logging must never break the sweep
            logger.exception("Failed to record sweep run for day %s", day)
