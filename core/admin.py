"""Django admin for SeraGo: manage sources, browse items, view the two log levels."""
import json

from django.contrib import admin
from django.utils.html import format_html, format_html_join

from .models import (
    AfriworkJob,
    AfriworkScrapeLog,
    EthioJobsJob,
    EthioJobsScrapeLog,
    HaHuJob,
    HaHuScrapeLog,
    ScrapeLog,
    ScrapedItem,
    Source,
)


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
        "afriwork_job",
        "ethiojobs_job",
        "hahujobs_job",
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


@admin.register(AfriworkScrapeLog)
class AfriworkScrapeLogAdmin(admin.ModelAdmin):
    """Per-website log — one row per (site, day) with every run that day."""

    ordering = ("-day",)
    list_display = (
        "day",
        "source",
        "status_colored",
        "run_count",
        "api_hits",
        "items_found",
        "items_inserted",
        "items_updated",
        "items_skipped",
        "updated_at",
    )
    list_filter = ("day", "source", "status")
    date_hierarchy = "day"
    readonly_fields = (
        "id",
        "source",
        "day",
        "status_colored",
        "run_count",
        "api_hits",
        "items_found",
        "items_inserted",
        "items_updated",
        "items_skipped",
        "scraped_log_pretty",
        "created_at",
        "updated_at",
    )
    list_select_related = ("source",)

    @admin.display(description="Status")
    def status_colored(self, obj):
        colors = {"success": "#28a745", "partial": "#ffc107", "failed": "#dc3545"}
        color = colors.get(obj.status, "#6c757d")
        return format_html('<span style="color: {}; font-weight: bold;">{}</span>', color, obj.status)

    @admin.display(description="scraped_log (JSON)")
    def scraped_log_pretty(self, obj):
        return format_html("<pre>{}</pre>", json.dumps(obj.scraped_log, indent=2, default=str))


@admin.register(AfriworkJob)
class AfriworkJobAdmin(admin.ModelAdmin):
    ordering = ("-numbered_on", "job_number")
    list_display = (
        "job_number_display",
        "title",
        "entity_name",
        "location",
        "job_type",
        "job_site",
        "experience_level",
        "approval_status",
        "published_at",
        "numbered_on",
    )
    list_filter = ("job_type", "job_site", "experience_level", "numbered_on")
    search_fields = ("title", "location", "entity_name", "external_id")
    readonly_fields = ("id", "job_number", "numbered_on", "created_at", "updated_at")


@admin.register(EthioJobsScrapeLog)
class EthioJobsScrapeLogAdmin(admin.ModelAdmin):
    """Per-website log — one row per (site, day) with every run that day."""

    ordering = ("-day",)
    list_display = (
        "day",
        "source",
        "status_colored",
        "run_count",
        "api_hits",
        "items_found",
        "items_inserted",
        "items_updated",
        "items_skipped",
        "updated_at",
    )
    list_filter = ("day", "source", "status")
    date_hierarchy = "day"
    readonly_fields = (
        "id",
        "source",
        "day",
        "status_colored",
        "run_count",
        "api_hits",
        "items_found",
        "items_inserted",
        "items_updated",
        "items_skipped",
        "scraped_log_pretty",
        "created_at",
        "updated_at",
    )
    list_select_related = ("source",)

    @admin.display(description="Status")
    def status_colored(self, obj):
        colors = {"success": "#28a745", "partial": "#ffc107", "failed": "#dc3545"}
        color = colors.get(obj.status, "#6c757d")
        return format_html('<span style="color: {}; font-weight: bold;">{}</span>', color, obj.status)

    @admin.display(description="scraped_log (JSON)")
    def scraped_log_pretty(self, obj):
        return format_html("<pre>{}</pre>", json.dumps(obj.scraped_log, indent=2, default=str))


@admin.register(HaHuScrapeLog)
class HaHuScrapeLogAdmin(admin.ModelAdmin):
    """Per-website log — one row per (site, day) with every run that day."""

    ordering = ("-day",)
    list_display = (
        "day",
        "source",
        "status_colored",
        "run_count",
        "api_hits",
        "items_found",
        "items_inserted",
        "items_updated",
        "items_skipped",
        "updated_at",
    )
    list_filter = ("day", "source", "status")
    date_hierarchy = "day"
    readonly_fields = (
        "id",
        "source",
        "day",
        "status_colored",
        "run_count",
        "api_hits",
        "items_found",
        "items_inserted",
        "items_updated",
        "items_skipped",
        "scraped_log_pretty",
        "created_at",
        "updated_at",
    )
    list_select_related = ("source",)

    @admin.display(description="Status")
    def status_colored(self, obj):
        colors = {"success": "#28a745", "partial": "#ffc107", "failed": "#dc3545"}
        color = colors.get(obj.status, "#6c757d")
        return format_html('<span style="color: {}; font-weight: bold;">{}</span>', color, obj.status)

    @admin.display(description="scraped_log (JSON)")
    def scraped_log_pretty(self, obj):
        return format_html("<pre>{}</pre>", json.dumps(obj.scraped_log, indent=2, default=str))


@admin.register(HaHuJob)
class HaHuJobAdmin(admin.ModelAdmin):
    ordering = ("-numbered_on", "job_number")
    list_display = (
        "job_number_display",
        "title",
        "entity_name",
        "cities",
        "type",
        "source",
        "application_method",
        "approved_on",
        "numbered_on",
    )
    list_filter = ("type", "source", "application_method", "numbered_on")
    # NOTE: the cities JSONField is deliberately not searchable — admin search
    # would build a `cities__icontains` lookup, which JSONField doesn't support.
    search_fields = ("title", "entity_name", "external_id")
    readonly_fields = ("id", "job_number", "numbered_on", "created_at", "updated_at")

    @admin.display(description="Location")
    def cities(self, obj):
        return obj.cities_display or "—"


@admin.register(EthioJobsJob)
class EthioJobsJobAdmin(admin.ModelAdmin):
    ordering = ("-numbered_on", "job_number")
    list_display = (
        "job_number_display",
        "title",
        "company_name",
        "state",
        "job_type_label",
        "location_type",
        "application_method",
        "published_at",
        "numbered_on",
    )
    list_filter = ("location_type", "application_method", "numbered_on")
    search_fields = ("title", "state", "slug", "external_id")
    readonly_fields = ("id", "job_number", "numbered_on", "created_at", "updated_at")

    @admin.display(description="Company", ordering="company")
    def company_name(self, obj):
        company = obj.company or {}
        return company.get("name") or "—"

    @admin.display(description="Type")
    def job_type_label(self, obj):
        codes = {1: "FULL_TIME", 2: "PART_TIME", 3: "CONTRACT", 4: "INTERNSHIP", 5: "FREELANCE", 6: "TEMPORARY", 7: "REMOTE"}
        return codes.get(obj.type, f"{obj.type} (other)" if obj.type else "—")
