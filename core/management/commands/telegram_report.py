"""Send the day's scrape report to a Telegram bot.

Usage:
    manage.py telegram_report                # per-run mode (default)
    manage.py telegram_report --mode daily   # failure-only + end-of-day digest
    manage.py telegram_report --force        # always send (e.g. testing)
    manage.py telegram_report --skip-tests   # don't run the test suite on the digest
    manage.py telegram_report --day 2026-08-15

Modes (NOTIFY_MODE env var, or --mode):
  per-run  — send a report after every scrape run (the current default, since
             we run twice a day and want both reports).
  daily    — for high-frequency scraping later (e.g. every 30 min): send ONLY
             when a run had problems (failed/partial run or non-200 response),
             plus ONE end-of-day digest on the last run of the day
             (DAILY_DIGEST_UTC, default 20:30 UTC = 23:30 Addis Ababa).

The message is the FULL day report: the day's status, run/website/api totals,
per-website found/inserted/skipped numbers, and every failure or unexpected
response recorded that day. On the final run of the day the command also runs
the Django test suite once and appends the result (pass/fail, test count,
duration) — use --skip-tests to disable.

Config (env vars):
  TELEGRAM_BOT_TOKEN  — bot token from @BotFather (add as an Actions secret).
  TELEGRAM_CHAT_ID    — chat to deliver to: your user id, or a group id.
  NOTIFY_MODE         — "per-run" | "daily" (default "per-run").
  DAILY_DIGEST_UTC    — HH:MM UTC of the final scheduled run of the day
                        (default "20:30").

Run right after ``scrape_all`` — the GitHub Actions workflow calls it with
``if: always()`` so it still fires when a scrape run fails (that's exactly
when you want the ping). Exits non-zero if the message could not be
delivered, so a delivery failure is visible in the Actions log.
"""
import os
import re
import subprocess
import sys
import time

import httpx

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import ScrapeLog, ScrapeStatus
from core.reporting import api_issues_for_day

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
# Telegram caps messages at 4096 chars; stay comfortably under.
MAX_MESSAGE_LENGTH = 3800


class Command(BaseCommand):
    help = "Send the day's scrape report to a Telegram bot."

    def add_arguments(self, parser):
        parser.add_argument(
            "--day",
            default=None,
            help="Local day as YYYY-MM-DD (default: today).",
        )
        parser.add_argument(
            "--mode",
            choices=["per-run", "daily"],
            default=None,
            help="Notification mode (overrides NOTIFY_MODE env var).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Send even when daily mode would stay quiet.",
        )
        parser.add_argument(
            "--skip-tests",
            action="store_true",
            help="Don't run the test suite on the end-of-day digest.",
        )

    def handle(self, *args, **options):
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            self.stdout.write(
                self.style.WARNING(
                    "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping notification."
                )
            )
            return

        day = options["day"] or timezone.localdate().isoformat()
        mode = options["mode"] or os.environ.get("NOTIFY_MODE", "per-run").strip() or "per-run"
        is_digest_run = self._is_digest_run()

        text, has_issues = self._format_report(day)

        if mode == "daily" and not options["force"]:
            if not (has_issues or is_digest_run):
                self.stdout.write(
                    self.style.SUCCESS(
                        "Daily mode: run OK and not the end-of-day digest time — nothing sent."
                    )
                )
                return

        if is_digest_run:
            # The day's final run: this is the FULL day report — every website,
            # api hit, item count, and any failures across all of the day's runs.
            title = "📊 Full Day Report" + (" ⚠️" if has_issues else "")
        elif has_issues:
            title = "⚠️ Scrape Issues"
        else:
            title = "🤖 Scrape Report"

        message = f"🤖 SeraGo — {title} · {day}\n\n{text}"

        # The end-of-day report also runs the test suite once and reports it.
        if is_digest_run and not options["skip_tests"]:
            message += "\n\n🧪 Tests\n" + self._run_tests()

        if len(message) > MAX_MESSAGE_LENGTH:
            message = message[:MAX_MESSAGE_LENGTH] + "\n…(truncated)"

        try:
            self._send(token, chat_id, message)
        except Exception as exc:  # noqa: BLE001 — surface any delivery failure
            self.stderr.write(self.style.ERROR(f"Telegram delivery failed: {exc}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Report sent to Telegram."))

    def _format_report(self, day: str) -> tuple[str, bool]:
        """Build the day's report as a tidy message body.

        Returns (text, has_issues) — has_issues is True when the day has any
        failed/partial run, non-200 response, or no log at all.
        """
        master = ScrapeLog.objects.filter(day=day).first()
        if master is None:
            return (
                "No scrape log was recorded for this day — the scrape likely "
                "never started (check the Actions run).",
                True,
            )

        issues = api_issues_for_day(day)
        has_issues = bool(issues) or master.status != ScrapeStatus.SUCCESS
        status_icon = "✅" if master.status == ScrapeStatus.SUCCESS else "⚠️"

        lines = [
            f"📈 status  : {status_icon} {master.status.upper()}",
            f"   runs    : {master.run_count}   · websites: {master.websites_count}   · api hits: {master.api_hits}",
            f"   found   : {master.items_found}   · inserted: {master.items_inserted}   · updated: {master.items_updated}   · skipped: {master.items_skipped}",
        ]

        websites = master.websites or []
        if websites:
            lines.append("")
            lines.append("🌐 Websites")
            name_width = min(
                max(max(len(w.get("name") or w.get("source") or "") for w in websites), 8),
                26,
            )
            for w in websites:
                name = (w.get("name") or w.get("source") or "").ljust(name_width)
                wicon = "✅" if w.get("status") == ScrapeStatus.SUCCESS else "⚠️"
                lines.append(
                    f"{wicon} {name}  api {w.get('api_hits', 0)} · "
                    f"found {w.get('items_found', 0)} · inserted {w.get('items_inserted', 0)} · "
                    f"skipped {w.get('items_skipped', 0)}"
                )

        if issues:
            lines.append("")
            lines.append("⚠️ Issues")
            for issue in issues:
                if issue["kind"] == "http":
                    lines.append(
                        f"• HTTP {issue['http_status']} — {issue['website']} "
                        f"page={issue['page']} (log {issue['log_id']})"
                    )
                else:
                    message = (
                        (issue.get("message") or "").strip().splitlines()[0]
                        if issue.get("message")
                        else ""
                    )
                    lines.append(
                        f"• {issue['status'].upper()} — {issue['website']}: {message}"
                    )

        return "\n".join(lines), has_issues

    def _run_tests(self) -> str:
        """Run the test suite once and summarize the outcome for the digest."""
        self.stdout.write(
            self.style.MIGRATE_HEADING("Running the test suite for the daily report…")
        )
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, "manage.py", "test", "--noinput"],
                capture_output=True,
                text=True,
                timeout=900,
            )
        except subprocess.TimeoutExpired:
            return "⏱️ timed out after 15 minutes"
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ could not run: {exc}"

        elapsed = int(time.monotonic() - started)
        duration = f"{elapsed // 60}m {elapsed % 60:02d}s" if elapsed >= 60 else f"{elapsed}s"

        output = proc.stdout or ""
        match = re.search(r"Ran (\d+) tests?", output)
        count = match.group(1) if match else "?"

        if proc.returncode == 0 and re.search(r"\bOK\b", output):
            return f"✅ {count} passed · {duration}"

        failed = re.search(r"FAILED\s*\(([^)]*)\)", output)
        detail = f" ({failed.group(1)})" if failed else ""
        return f"❌ {count} tests FAILED{detail} · {duration} — see the Actions log"

    def _is_digest_run(self) -> bool:
        """True when the current run is at/after the day's final scheduled run."""
        digest = os.environ.get("DAILY_DIGEST_UTC", "20:30").strip()
        try:
            hour, minute = (int(part) for part in digest.split(":", 1))
        except ValueError:
            self.stdout.write(
                self.style.WARNING(
                    f"DAILY_DIGEST_UTC={digest!r} not HH:MM — defaulting to 20:30."
                )
            )
            hour, minute = 20, 30
        now = timezone.now()  # UTC-aware
        return (now.hour, now.minute) >= (hour, minute)

    def _send(self, token: str, chat_id: str, text: str) -> None:
        response = httpx.post(
            TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": text},
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description") or "Telegram API returned ok=false")
