"""Log-report helpers — surface any non-200 API response or failed run.

``api_issues_for_day`` walks every website's day log (the SITE_LOG_MODELS
registry) and collects anything that deserves attention: page hits whose
``http_status`` was not 200 (which website, which page, which log row) and
runs whose status was ``failed``/``partial`` (with their errors/message).
``defaulted_deadline_summary`` surfaces listings whose deadline was defaulted
by the scraper (the source provided none) so the source mapping can be fixed.
``silent_zero_sources_for_day`` flags sources that logged clean success but
found nothing all day (non-Sundays) — the ReporterJobs JS-skeleton failure
mode would otherwise hide behind an empty, successful day.
``recompute_stat`` / ``update_current_stats`` maintain the PERSISTENT
``ScrapeStat`` rollups at day/week/month/year grain (never deleted — the
YEAR row aggregates the never-deleted MONTH rows), and ``stat_block`` renders
them for the daily report. ``recompute_sector_stats`` keeps the PERSISTENT
day-granular ``CategoryStat`` sector counts the SeraGo stats dashboard reads.
Used by the ``log_report`` / ``telegram_report`` management commands and by
``manage.py check``'s test suite.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta

from django.utils import timezone

from core.models import (
    SITE_LOG_MODELS,
    AfriworkJob,
    CategoryStat,
    HaHuJob,
    ScrapeLog,
    ScrapeStat,
    ScrapeStatus,
    ScrapedItem,
)


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


def silent_zero_sources_for_day(day: date | None = None) -> list[dict]:
    """Sources whose day looks like a SILENT failure: clean success, 0 items.

    A source that logged only successful runs and found nothing all day may
    be legitimately quiet — HaHu (the aggregator) posts nothing on Sundays —
    or silently broken, like ReporterJobs' JS-skeleton case where the run
    said ``success`` and stored nothing for days at a time. Sundays are
    excluded because that is the one day HaHu legitimately posts nothing.
    Returns one dict per suspicious source
    (``website`` / ``name`` / ``run_count`` / ``api_hits``).
    """
    day = day or timezone.localdate()
    if day.weekday() == 6:  # Sunday — HaHu legitimately posts nothing
        return []
    flagged: list[dict] = []
    for model in SITE_LOG_MODELS:
        site_logs = model.objects.select_related("source").filter(day=day)
        for site_log in site_logs:
            if (
                site_log.run_count > 0
                and site_log.status == ScrapeStatus.SUCCESS
                and site_log.items_found == 0
            ):
                flagged.append(
                    {
                        "website": site_log.source.slug,
                        "name": site_log.source.name,
                        "run_count": site_log.run_count,
                        "api_hits": site_log.api_hits,
                    }
                )
    return flagged


def defaulted_deadline_summary(max_per_source: int = 3) -> list[str]:
    """Human-readable lines about listings whose deadline was defaulted.

    The scraper gives a listing without a source deadline ``published + 30
    days`` and flags it (``deadline_is_default``). This surfaces those rows
    so the source's field mapping can be fixed; once a real deadline arrives
    the flag clears and the listing stops appearing here. Returns one line
    per affected source, e.g.::

        EthioJobs: 4 active job(s) — liRI5jhRRL-sales-manager, ...

    Empty list when nothing is defaulted.
    """
    rows = (
        ScrapedItem.objects.filter(deadline_is_default=True, is_active=True)
        .select_related("source")
        .order_by("source__name", "external_id")
    )
    by_source: dict[str, list[str]] = {}
    for item in rows.iterator():
        by_source.setdefault(item.source.name, []).append(str(item.external_id))
    lines: list[str] = []
    for source_name, ids in sorted(by_source.items()):
        shown = ", ".join(ids[:max_per_source])
        more = f" (+{len(ids) - max_per_source} more)" if len(ids) > max_per_source else ""
        lines.append(f"{source_name}: {len(ids)} job(s) — {shown}{more}")
    return lines


def week_bounds(day: date) -> tuple[date, date]:
    """(Monday, Sunday) of the calendar week containing ``day``."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def month_bounds(day: date) -> tuple[date, date]:
    """(1st, last day) of the calendar month containing ``day``."""
    start = day.replace(day=1)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    return start, next_month - timedelta(days=1)

def year_bounds(day: date) -> tuple[date, date]:
    """(Jan 1, Dec 31) of the calendar year containing ``day``."""
    start = day.replace(month=1, day=1)
    return start, start.replace(month=12, day=31)


def recompute_stat(period_type: str, period_start: date) -> ScrapeStat:
    """Recompute a day/week/month ScrapeStat row from the day logs and upsert it.

    Aggregates come from the day-level rows (a week is ~7 master rows +
    ~35 site rows), so this is cheap and idempotent — calling it again
    simply overwrites the same row. Runs-by-status and the top error
    messages are counted from each site's ``scraped_log`` run entries. YEAR
    rows are special: the day logs for past months are pruned by the weekly
    archive, so a year is aggregated from the never-deleted MONTH rows
    instead (see :func:`_recompute_year_stat`).
    """
    if period_type == ScrapeStat.PeriodType.YEAR:
        return _recompute_year_stat(period_start)
    if period_type == ScrapeStat.PeriodType.DAY:
        start = end = period_start
    elif period_type == ScrapeStat.PeriodType.WEEK:
        start, end = week_bounds(period_start)
    else:
        start, end = month_bounds(period_start)

    masters = list(ScrapeLog.objects.filter(day__gte=start, day__lte=end))
    run_count = sum(m.run_count for m in masters)
    api_hits = sum(m.api_hits for m in masters)
    items_found = sum(m.items_found for m in masters)
    items_inserted = sum(m.items_inserted for m in masters)
    items_updated = sum(m.items_updated for m in masters)
    items_skipped = sum(m.items_skipped for m in masters)

    by_source: dict[str, dict] = {}
    runs_by_status: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    for model in SITE_LOG_MODELS:
        site_rows = model.objects.select_related("source").filter(
            day__gte=start, day__lte=end
        )
        for row in site_rows:
            slug = row.source.slug
            src = by_source.setdefault(
                slug,
                {
                    "run_count": 0,
                    "api_hits": 0,
                    "items_found": 0,
                    "items_inserted": 0,
                    "items_updated": 0,
                    "items_skipped": 0,
                    "days_with_runs": 0,
                    "failed_runs": 0,
                },
            )
            src["run_count"] += row.run_count
            src["api_hits"] += row.api_hits
            src["items_found"] += row.items_found
            src["items_inserted"] += row.items_inserted
            src["items_updated"] += row.items_updated
            src["items_skipped"] += row.items_skipped
            src["days_with_runs"] += 1
            for run in row.scraped_log or []:
                status = run.get("status")
                if status:
                    runs_by_status[status] += 1
                if status in ("failed", "partial"):
                    src["failed_runs"] += 1
                for error in run.get("errors") or []:
                    if isinstance(error, str) and error.strip():
                        error_counts[error.strip()] += 1

    # Distinct counting happens on the FULL message (different URLs stay
    # separate); the stored text is cleaned so the report stays tight
    # (httpx errors carry long 'For more information check: …' boilerplate
    # that adds nothing).
    def _clean(message: str) -> str:
        marker = "For more information check:"
        if marker in message:
            message = message.split(marker, 1)[0].rstrip()
        return message[:120]

    top_errors = [
        {"message": _clean(message), "count": count}
        for message, count in error_counts.most_common(5)
    ]

    stat, _ = ScrapeStat.objects.update_or_create(
        period_type=period_type,
        period_start=start,
        defaults={
            "period_end": end,
            "days_with_runs": len(masters),
            "run_count": run_count,
            "api_hits": api_hits,
            "items_found": items_found,
            "items_inserted": items_inserted,
            "items_updated": items_updated,
            "items_skipped": items_skipped,
            "runs_by_status": dict(runs_by_status),
            "top_errors": top_errors,
            "by_source": by_source,
        },
    )
    return stat


def _recompute_year_stat(year_start: date) -> ScrapeStat:
    """Aggregate a YEAR ScrapeStat row from the stored MONTH rows.

    The day logs for past months are pruned by the weekly archive, so a year
    cannot be recomputed from them — but the MONTH rows are never deleted, so
    summing them is exact (a month before stats were recorded contributes
    nothing).
    """
    start, end = year_bounds(year_start)
    months = ScrapeStat.objects.filter(
        period_type=ScrapeStat.PeriodType.MONTH,
        period_start__gte=start,
        period_start__lte=end,
    )
    run_count = api_hits = items_found = 0
    items_inserted = items_updated = items_skipped = days_with_runs = 0
    runs_by_status: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    by_source: dict[str, dict] = {}
    for month in months:
        run_count += month.run_count
        api_hits += month.api_hits
        items_found += month.items_found
        items_inserted += month.items_inserted
        items_updated += month.items_updated
        items_skipped += month.items_skipped
        days_with_runs += month.days_with_runs
        for status, count in (month.runs_by_status or {}).items():
            runs_by_status[status] += count
        for error in month.top_errors or []:
            error_counts[error["message"]] += error["count"]
        for slug, src in (month.by_source or {}).items():
            target = by_source.setdefault(
                slug,
                {
                    "run_count": 0,
                    "api_hits": 0,
                    "items_found": 0,
                    "items_inserted": 0,
                    "items_updated": 0,
                    "items_skipped": 0,
                    "days_with_runs": 0,
                    "failed_runs": 0,
                },
            )
            for key in (
                "run_count",
                "api_hits",
                "items_found",
                "items_inserted",
                "items_updated",
                "items_skipped",
                "days_with_runs",
                "failed_runs",
            ):
                target[key] += src.get(key, 0)
    top_errors = [
        {"message": message, "count": count}
        for message, count in error_counts.most_common(5)
    ]
    stat, _ = ScrapeStat.objects.update_or_create(
        period_type=ScrapeStat.PeriodType.YEAR,
        period_start=start,
        defaults={
            "period_end": end,
            "days_with_runs": days_with_runs,
            "run_count": run_count,
            "api_hits": api_hits,
            "items_found": items_found,
            "items_inserted": items_inserted,
            "items_updated": items_updated,
            "items_skipped": items_skipped,
            "runs_by_status": dict(runs_by_status),
            "top_errors": top_errors,
            "by_source": by_source,
        },
    )
    return stat


#: Registry of (detail model, posting-date field, sector-name extractor) for
#: the top-sectors stat. Only sites that expose a sector on their listings
#: appear here — adding a new site (or a new normalized dimension later,
#: e.g. "job" / "company") is one line. The extractor returns a list of
#: sector names; unknown/empty names are dropped.
SECTOR_EXTRACTORS = [
    (AfriworkJob, "published_at", lambda row: row.sectors or []),
    (HaHuJob, "approved_on", lambda row: [row.sector_name] if row.sector_name else []),
]


def recompute_sector_stats(day: date) -> None:
    """Upsert the sector counts for one day from that day's detail rows.

    A day's detail rows exist until the weekly archive prunes them, so this
    is called while the day is current (after every scrape and at archive
    time). Week/month/year figures are derived by summing these day rows, so
    the history survives the prune. Idempotent — re-running overwrites the
    day's counts and drops sectors that no longer appear.
    """
    counts: Counter[str] = Counter()
    # The day is the local (Addis) day; filter by the exact aware range so an
    # item published late in the day doesn't fall into the wrong day's bucket
    # (the DB stores timestamps in UTC, where ``__date`` would slice by UTC).
    day_start = timezone.make_aware(datetime.combine(day, datetime.min.time()))
    day_end = day_start + timedelta(days=1)
    for model, date_field, extract in SECTOR_EXTRACTORS:
        rows = model.objects.filter(
            **{
                f"{date_field}__gte": day_start,
                f"{date_field}__lt": day_end,
            }
        )
        for row in rows.iterator():
            for sector in extract(row):
                cleaned = " ".join(str(sector).split())
                if cleaned:
                    counts[cleaned] += 1
    for name, count in counts.items():
        CategoryStat.objects.update_or_create(
            category_type="sector",
            period_start=day,
            category_name=name,
            defaults={"count": count},
        )
    # A sector that no longer appears after a re-scrape must not keep its old
    # count (or linger as a stale row when the day ended with zero items).
    CategoryStat.objects.filter(category_type="sector", period_start=day).exclude(
        category_name__in=list(counts)
    ).delete()


def update_current_stats() -> None:
    """Upsert the current day/week/month/year ScrapeStat rows + today's
    sector counts.

    Called after every ``scrape_all`` and again by the archive before it
    deletes the logs — so the persistent stats are always final even though
    the underlying day logs and detail rows are archived and cleared.
    """
    today = timezone.localdate()
    recompute_stat(ScrapeStat.PeriodType.DAY, today)
    recompute_stat(ScrapeStat.PeriodType.WEEK, week_bounds(today)[0])
    recompute_stat(ScrapeStat.PeriodType.MONTH, month_bounds(today)[0])
    recompute_stat(ScrapeStat.PeriodType.YEAR, year_bounds(today)[0])
    recompute_sector_stats(today)


def stat_block(period_type: str, period_start: date) -> list[str]:
    """Human-readable lines for a stored ScrapeStat row (empty if none).

    Rendered as a "📊 WEEK / 📊 MONTH" section in the daily report: the
    period's totals, the run-status breakdown, one short line per source,
    and the most common error messages.
    """
    stat = ScrapeStat.objects.filter(
        period_type=period_type, period_start=period_start
    ).first()
    if stat is None:
        return []

    label = dict(ScrapeStat.PeriodType.choices).get(period_type, period_type)
    lines = [
        f"📊 {label.upper()} {stat.period_start} → {stat.period_end}",
        f"   {stat.days_with_runs} day(s) · {stat.run_count} run(s) · api {stat.api_hits}",
        f"   found {stat.items_found} · inserted {stat.items_inserted} · "
        f"updated {stat.items_updated} · skipped {stat.items_skipped}",
    ]
    by_status = stat.runs_by_status or {}
    status_parts = []
    if by_status.get("success"):
        status_parts.append(f"✅ {by_status['success']} success")
    if by_status.get("partial"):
        status_parts.append(f"⚠️ {by_status['partial']} partial")
    if by_status.get("failed"):
        status_parts.append(f"❌ {by_status['failed']} failed")
    if status_parts:
        lines.append("   " + " · ".join(status_parts))

    for slug in sorted(stat.by_source or {}):
        src = stat.by_source[slug]
        failed = src.get("failed_runs", 0)
        flag = f" · ❌ {failed} failed" if failed else ""
        lines.append(
            f"   {slug}: {src.get('items_inserted', 0)} inserted · "
            f"{src.get('items_found', 0)} found · {src.get('run_count', 0)} runs{flag}"
        )

    for error in stat.top_errors or []:
        lines.append(f"   ❌ {error['message']} (×{error['count']})")
    return lines
