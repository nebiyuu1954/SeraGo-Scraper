"""Print a report of the day's scrape logs.

Usage:
    manage.py log_report              # today
    manage.py log_report --day 2026-08-07
    manage.py log_report --all        # every day present in the logs

Output: the master day totals, one short line per website, then any issues —
page hits that did not return HTTP 200 (which website, which page, which log
row) and failed/partial runs (with their errors). Enough to manage every
website from a single command.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import SITE_LOG_MODELS, ScrapeLog
from core.reporting import api_issues_for_day, defaulted_deadline_summary


class Command(BaseCommand):
    help = "Report the day's scrape logs: totals, per-website numbers, and API issues."

    def add_arguments(self, parser):
        parser.add_argument(
            "--day",
            default=None,
            help="Local day as YYYY-MM-DD (default: today).",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Report every day present in the logs.",
        )

    def handle(self, *args, **options):
        if options["all"]:
            days = list(
                ScrapeLog.objects.order_by("-day").values_list("day", flat=True)
            )
        else:
            days = [options["day"] or timezone.localdate().isoformat()]
        if not days:
            self.stdout.write("No scrape logs found.")
            return
        for day in days:
            self._report_day(day)

    def _report_day(self, day):
        master = ScrapeLog.objects.filter(day=day).first()
        if master is None:
            self.stdout.write(self.style.WARNING(f"--- {day}: no master log ---"))
            return

        issues = api_issues_for_day(day)
        self.stdout.write(self.style.MIGRATE_HEADING(f"--- {day} ---"))
        self.stdout.write(
            f"status: {master.status} | runs: {master.run_count} | "
            f"websites: {master.websites_count} | api_hits: {master.api_hits}"
        )
        self.stdout.write(
            f"items: found={master.items_found} inserted={master.items_inserted} "
            f"updated={master.items_updated} skipped={master.items_skipped}"
        )
        for website in master.websites:
            self.stdout.write(
                f"  {website.get('name') or website.get('source')}: "
                f"{website.get('status')} | runs={website.get('run_count')} "
                f"api_hits={website.get('api_hits')} "
                f"found={website.get('items_found')} "
                f"inserted={website.get('items_inserted')} "
                f"updated={website.get('items_updated')} "
                f"skipped={website.get('items_skipped')}"
            )

        defaulted = defaulted_deadline_summary()
        if defaulted:
            self.stdout.write(
                self.style.WARNING("  Defaulted deadlines (source gave no deadline; +30d set):")
            )
            for line in defaulted:
                self.stdout.write(f"    - {line}")

        if not issues:
            site_log_count = sum(
                model.objects.filter(day=day).count() for model in SITE_LOG_MODELS
            )
            if site_log_count == 0:
                self.stdout.write(
                    self.style.WARNING(
                        "  No website logs recorded for this day (detail-log hook may have failed)."
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS("  All API responses returned 200; no failed runs.")
                )
            return
        for issue in issues:
            if issue["kind"] == "http":
                self.stdout.write(
                    self.style.ERROR(
                        f"  [HTTP {issue['http_status']}] {issue['website']} "
                        f"page={issue['page']} (log {issue['log_id']}, "
                        f"started {issue['run_started_at']})"
                    )
                )
            else:
                self.stdout.write(
                    self.style.ERROR(
                        f"  [{issue['status'].upper()}] {issue['website']} "
                        f"run started {issue['run_started_at']}: "
                        f"{issue['message']} errors={issue['errors']}"
                    )
                )
