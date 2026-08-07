"""Log-report helpers — surface any non-200 API response or failed run.

``api_issues_for_day`` walks every website's day log (the SITE_LOG_MODELS
registry) and collects anything that deserves attention: page hits whose
``http_status`` was not 200 (which website, which page, which log row) and
runs whose status was ``failed``/``partial`` (with their errors/message).
Used by the ``log_report`` management command and by the test suite.
"""
from __future__ import annotations

from datetime import date

from django.utils import timezone

from core.models import SITE_LOG_MODELS


def api_issues_for_day(day: date | None = None) -> list[dict]:
    """Every API/run problem recorded across all website logs for a day."""
    day = day or timezone.localdate()
    issues: list[dict] = []
    for model in SITE_LOG_MODELS:
        site_logs = model.objects.select_related("source").filter(day=day)
        for site_log in site_logs:
            for run in site_log.scraped_log or []:
                for page in run.get("pages_hit") or []:
                    http_status = page.get("http_status")
                    # Compare as strings so a malformed value is flagged as an
                    # issue instead of crashing the whole report.
                    if http_status is not None and str(http_status) != "200":
                        issues.append(
                            {
                                "kind": "http",
                                "website": site_log.source.slug,
                                "table": model.__name__,
                                "log_id": str(site_log.pk),
                                "run_started_at": run.get("started_at"),
                                "page": page.get("page"),
                                "http_status": http_status,
                            }
                        )
                if run.get("status") in ("failed", "partial"):
                    issues.append(
                        {
                            "kind": "run",
                            "website": site_log.source.slug,
                            "table": model.__name__,
                            "log_id": str(site_log.pk),
                            "run_started_at": run.get("started_at"),
                            "status": run.get("status"),
                            "errors": run.get("errors") or [],
                            "message": run.get("message") or "",
                        }
                    )
    return issues
