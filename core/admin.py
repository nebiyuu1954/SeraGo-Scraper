"""Django admin for SeraGo: manage sources, browse items, view the two log levels."""
import json

from django.contrib import admin
from django.utils.html import format_html, format_html_join

from .models import (
    CategoryStat,
    ScrapeLog,
    ScrapeStat,
    ScrapedItem,
    Source,
    SITE_LOG_MODELS,
    SectorClassificationRule,
)

@admin.register(SectorClassificationRule)
class SectorClassificationRuleAdmin(admin.ModelAdmin):
    list_display = ("rule_text", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("rule_text",)


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "scraper_type",
        "is_active",
        "scrape_interval_hours",
        "last_success_at",
    )
    list_filter = ("scraper_type", "is_active")
    search_fields = ("name", "slug", "endpoint")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("id", "created_at", "updated_at", "last_scraped_at", "last_success_at")


@admin.register(ScrapedItem)
class ScrapedItemAdmin(admin.ModelAdmin):
    # Explicit order: newest day first, then #01, #02, ... within the day.
    ordering = ("-numbered_on", "job_number")
    list_display = (
        "job_number_display",
        "title",
        "source",
        "location",
        "job_type",
        "published_at",
        "numbered_on",
        "is_active",
    )
    list_filter = ("source", "job_type", "is_active", "numbered_on")
    search_fields = ("title", "company", "location", "external_id")
    readonly_fields = (
        "id",
        "job_number",
        "numbered_on",
        "content_hash",
        "first_seen_at",
        "last_seen_at",
        "created_at",
        "updated_at",
    )
    list_select_related = ("source",)


@admin.register(ScrapeLog)
class ScrapeLogAdmin(admin.ModelAdmin):
    """MASTER log — one row per day referencing every website's logs."""

    ordering = ("-day",)
    list_display = (
        "day",
        "status_colored",
        "run_count",
        "websites_count",
        "api_hits",
        "items_found",
        "items_inserted",
        "items_updated",
        "items_skipped",
        "websites_summary",
        "updated_at",
    )
    list_filter = ("status", "day")
    date_hierarchy = "day"
    exclude = ("websites", "runs")
    readonly_fields = (
        "id",
        "day",
        "status_colored",
        "run_count",
        "websites_count",
        "api_hits",
        "items_found",
        "items_inserted",
        "items_updated",
        "items_skipped",
        "websites_pretty",
        "runs_pretty",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Status")
    def status_colored(self, obj):
        colors = {"success": "#28a745", "partial": "#ffc107", "failed": "#dc3545"}
        color = colors.get(obj.status, "#6c757d")
        return format_html('<span style="color: {}; font-weight: bold;">{}</span>', color, obj.status)

    @admin.display(description="Websites")
    def websites_summary(self, obj):
        if not obj.websites:
            return "—"
        rows = (
            (w.get("name") or w.get("source") or "?", w.get("run_count", 0))
            for w in obj.websites
        )
        return format_html_join("<br>", "<b>{}</b>: {} run(s)", rows)

    @admin.display(description="Websites (JSON)")
    def websites_pretty(self, obj):
        return format_html("<pre>{}</pre>", json.dumps(obj.websites, indent=2, default=str))

    @admin.display(description="Runs (JSON)")
    def runs_pretty(self, obj):
        reversed_runs = list(reversed(obj.runs)) if obj.runs else []
        return format_html("<pre>{}</pre>", json.dumps(reversed_runs, indent=2, default=str))


@admin.register(ScrapeStat)
class ScrapeStatAdmin(admin.ModelAdmin):
    """PERSISTENT weekly/monthly rollups — never deleted (unlike the logs)."""

    ordering = ("-period_start",)
    list_display = (
        "period_type",
        "period_start",
        "period_end",
        "days_with_runs",
        "run_count",
        "api_hits",
        "items_found",
        "items_inserted",
        "items_updated",
        "items_skipped",
        "updated_at",
    )
    list_filter = ("period_type",)
    readonly_fields = (
        "id",
        "period_type",
        "period_start",
        "period_end",
        "days_with_runs",
        "run_count",
        "api_hits",
        "items_found",
        "items_inserted",
        "items_updated",
        "items_skipped",
        "runs_by_status",
        "top_errors",
        "by_source",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False  # stats are computed, never hand-created

    def has_change_permission(self, request, obj=None):
        return False  # computed read-only


@admin.register(CategoryStat)
class CategoryStatAdmin(admin.ModelAdmin):
    """PERSISTENT day-granular category counts (sectors) — never deleted."""

    ordering = ("-period_start", "-count")
    list_display = (
        "category_type",
        "period_start",
        "category_name",
        "count",
        "updated_at",
    )
    list_filter = ("category_type",)
    search_fields = ("category_name",)
    readonly_fields = ("id", "category_type", "period_start", "category_name", "count", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False  # stats are computed, never hand-created

    def has_change_permission(self, request, obj=None):
        return False  # computed read-only

@admin.display(description="Scraped log (JSON)")
def json_pretty(self, obj):
    return format_html("<pre>{}</pre>", json.dumps(obj.scraped_log, indent=2, default=str))

for model in SITE_LOG_MODELS:
    admin.site.register(
        model,
        type(
            'SiteLogAdmin',
            (admin.ModelAdmin,),
            {
                'ordering': ('-day',),
                'list_display': ('day', 'source', 'status', 'run_count', 'api_hits', 'items_found', 'items_inserted', 'updated_at'),
                'list_filter': ('status', 'source'),
                'readonly_fields': ('day', 'source', 'status', 'run_count', 'api_hits', 'items_found', 'items_inserted', 'items_updated', 'items_skipped', 'json_pretty', 'created_at', 'updated_at'),
                'exclude': ('scraped_log',),
                'json_pretty': json_pretty,
            }
        )
    )
