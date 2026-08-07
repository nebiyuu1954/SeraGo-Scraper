"""One-command daily health check for SeraGo.

Usage:
    manage.py daily_check                # tests + live structure check + today's log report
    manage.py daily_check --day 2026-08-07
    manage.py daily_check --skip-tests    # skip the test suite (faster)

Runs in order:
1. The test suite — structure snapshots, API-status detection, day-log rollups.
2. Live structure comparison for every active source against its snapshot
   (``check_structure`` — catches websites that changed their API).
3. Today's log report (``log_report``) — totals, per-website numbers, and any
   non-200 responses / failed runs.

Exit code is non-zero if any test fails or any structure changed.
"""
import subprocess
import sys

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "One-command daily check: tests, live structure check, and today's log report."

    def add_arguments(self, parser):
        parser.add_argument(
            "--day",
            default=None,
            help="Day to report as YYYY-MM-DD (default: today).",
        )
        parser.add_argument(
            "--skip-tests",
            action="store_true",
            help="Skip the test suite (faster).",
        )

    def handle(self, *args, **options):
        failures = 0

        if not options["skip_tests"]:
            self.stdout.write(self.style.MIGRATE_HEADING("== 1/3 Test suite =="))
            proc = subprocess.run(
                [sys.executable, "manage.py", "test", "core", "--verbosity", "1"]
            )
            if proc.returncode != 0:
                failures += 1
                self.stdout.write(self.style.ERROR("-> Test suite FAILED."))
            else:
                self.stdout.write(self.style.SUCCESS("-> Test suite passed."))
        else:
            self.stdout.write(self.style.WARNING("== 1/3 Tests skipped (--skip-tests) =="))

        self.stdout.write(self.style.MIGRATE_HEADING("== 2/3 Live structure check =="))
        try:
            call_command("check_structure")
        except CommandError:
            failures += 1

        self.stdout.write(self.style.MIGRATE_HEADING("== 3/3 Log report =="))
        call_command(
            "log_report", day=options["day"] or timezone.localdate().isoformat()
        )

        self.stdout.write(self.style.MIGRATE_HEADING("== Result =="))
        if failures:
            self.stdout.write(
                self.style.ERROR(f"{failures} check(s) failed — see output above.")
            )
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("All checks passed."))
