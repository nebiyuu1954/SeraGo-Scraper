"""Send the day's scrape report to a Telegram bot.

Usage:
    manage.py telegram_report                # per-run mode (default)
    manage.py telegram_report --mode daily   # failure-only + end-of-day digest
    manage.py telegram_report --force        # always send (e.g. testing)
    manage.py telegram_report --day 2026-08-15

Modes (NOTIFY_MODE env var, or --mode):
  per-run  — send a report after every scrape run (the current default, since
             we run twice a day and want both reports).
  daily    — for high-frequency scraping later (e.g. every 30 min): send ONLY
             when a run had problems (failed/partial run or non-200 response),
             plus ONE end-of-day digest on the last run of the day
             (DAILY_DIGEST_UTC, default 20:30 UTC = 23:30 Addis Ababa). The
             digest is the FULL day report — every website, api hit, item
             counts, the day's status, and any failures across all runs.

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
import io
import os

import httpx

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

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

        text = self._build_report(day)
        has_issues = self._has_issues(day, text)
        is_digest_run = self._is_digest_run()

        if mode == "daily" and not options["force"]:
            if not (has_issues or is_digest_run):
                self.stdout.write(
                    self.style.SUCCESS(
                        "Daily mode: run OK and not the end-of-day digest time — nothing sent."
                    )
                )
                return

        if is_digest_run:
            # The day's final run: the message is the FULL day report — every
            # website, api hits, item counts, and any failures across all of
            # the day's runs (not just this last one).
            header = "📊 SeraGo — full day report" + (" ⚠️" if has_issues else "")
        elif has_issues:
            header = "⚠️ SeraGo — scrape issues"
        else:
            header = "🤖 SeraGo — scrape report"
        message = f"{header} · {day}\n\n{text}"
        if len(message) > MAX_MESSAGE_LENGTH:
            message = message[:MAX_MESSAGE_LENGTH] + "\n…(truncated)"

        try:
            self._send(token, chat_id, message)
        except Exception as exc:  # noqa: BLE001 — surface any delivery failure
            self.stderr.write(self.style.ERROR(f"Telegram delivery failed: {exc}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Report sent to Telegram."))

    def _build_report(self, day: str) -> str:
        """Reuse log_report's output as the message body (stays in sync)."""
        buf = io.StringIO()
        call_command("log_report", day=day, stdout=buf)
        return buf.getvalue().strip()

    def _has_issues(self, day: str, text: str) -> bool:
        """Anything worth flagging: recorded run issues, or no logs at all."""
        if api_issues_for_day(day):
            return True
        return any(marker in text for marker in ("status: failed", "no master log"))

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
