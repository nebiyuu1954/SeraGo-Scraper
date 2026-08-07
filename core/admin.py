"""Django admin for SeraGo: manage sources, browse items, view scrape logs."""
from django.contrib import admin

from .models import ScrapeLog, ScrapedItem, Source


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
    list_display = (
        "title",
        "source",
        "location",
        "job_type",
        "published_at",
        "last_seen_at",
        "is_active",
    )
    list_filter = ("source", "job_type", "is_active")
    search_fields = ("title", "company", "location", "external_id")
    readonly_fields = ("id", "content_hash", "first_seen_at", "last_seen_at", "created_at", "updated_at")
    list_select_related = ("source",)


@admin.register(ScrapeLog)
class ScrapeLogAdmin(admin.ModelAdmin):
    list_display = (
        "source",
        "status",
        "page",
        "items_found",
        "items_inserted",
        "items_updated",
        "items_skipped",
        "duration_ms",
        "started_at",
    )
    list_filter = ("status", "source")
    readonly_fields = ("id", "started_at", "finished_at", "created_at", "updated_at")
    list_select_related = ("source",)
