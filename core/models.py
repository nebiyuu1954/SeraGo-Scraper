"""Core models for SeraGo: Source, ScrapedItem and ScrapeLog.

These tables are the contract shared with the .NET backend via PostgreSQL:
sources, scraped_items, scrape_logs (health_results comes with the Celery step).
"""
import uuid

from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base: UUID primary key plus created/updated timestamps."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ScraperType(models.TextChoices):
    GRAPHQL = "graphql", "GraphQL (Hasura)"
    NEXTJS = "nextjs", "Next.js data API"
    HTML = "html", "Server-side HTML"
    PLAYWRIGHT = "playwright", "Headless browser (Playwright)"


class JobType(models.TextChoices):
    FULL_TIME = "FULL_TIME", "Full-time"
    PART_TIME = "PART_TIME", "Part-time"
    CONTRACT = "CONTRACT", "Contract"
    CONTRACTUAL = "CONTRACTUAL", "Contractual"
    REMOTE = "REMOTE", "Remote"
    INTERNSHIP = "INTERNSHIP", "Internship"
    FREELANCE = "FREELANCE", "Freelance"
    TEMPORARY = "TEMPORARY", "Temporary"
    OTHER = "OTHER", "Other"


class ScrapeStatus(models.TextChoices):
    RUNNING = "running", "Running"
    SUCCESS = "success", "Success"
    PARTIAL = "partial", "Partial"
    FAILED = "failed", "Failed"


class Source(TimeStampedModel):
    """A scrapable job website — fully configuration-driven scraping.

    Holds the endpoint, request headers, query, field mapping (dotted paths)
    and pagination rules so adding a new source requires zero code changes.
    """

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, unique=True)
    base_url = models.URLField(max_length=500, blank=True, default="")
    scraper_type = models.CharField(
        max_length=20,
        choices=ScraperType.choices,
        default=ScraperType.GRAPHQL,
    )
    endpoint = models.URLField(max_length=500)
    headers = models.JSONField(
        default=dict,
        blank=True,
        help_text="HTTP headers sent with every request to this source.",
    )
    query = models.TextField(
        blank=True,
        default="",
        help_text="GraphQL query (or API params) used to fetch listings.",
    )
    field_mapping = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Normalized field -> dotted path, e.g. {'external_id': 'id', "
            "'title': 'title', 'description': {'path': 'description', "
            "'transforms': ['strip_html']}}."
        ),
    )
    pagination = models.JSONField(
        default=dict,
        blank=True,
        help_text="Pagination rules: page_size, results_path, variables, timeout...",
    )
    scrape_interval_hours = models.PositiveIntegerField(default=24)
    is_active = models.BooleanField(default=True)
    last_scraped_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["scraper_type", "is_active"], name="src_type_active_idx"),
        ]

    def __str__(self):
        return self.name


class ScrapedItem(TimeStampedModel):
    """A normalized job listing. Deduplicated per (source, external_id)."""

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="items")
    external_id = models.CharField(max_length=255, help_text="The source's own id for this listing.")
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True, default="")
    company = models.CharField(max_length=255, blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="")
    job_type = models.CharField(max_length=32, choices=JobType.choices, blank=True, default="")
    url = models.URLField(max_length=1000, blank=True, default="")
    salary = models.CharField(max_length=255, blank=True, default="")
    content_hash = models.CharField(
        max_length=64,
        db_index=True,
        help_text="SHA-256 of title|company|location|job_type.",
    )
    raw_data = models.JSONField(default=dict, blank=True, help_text="Original payload for this item.")
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deadline = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True, help_text="False once the listing disappears from the source.")

    class Meta:
        ordering = ["-published_at"]
        constraints = [
            models.UniqueConstraint(fields=["source", "external_id"], name="uniq_source_external_id"),
        ]
        indexes = [
            models.Index(fields=["source", "is_active"], name="item_src_active_idx"),
        ]

    def __str__(self):
        return self.title


class ScrapeLog(TimeStampedModel):
    """Audit trail for one scrape run of one source (status, counts, errors)."""

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="logs")
    status = models.CharField(
        max_length=16,
        choices=ScrapeStatus.choices,
        default=ScrapeStatus.RUNNING,
    )
    page = models.PositiveIntegerField(default=0, help_text="0-based page/offset index.")
    items_found = models.PositiveIntegerField(default=0)
    items_inserted = models.PositiveIntegerField(default=0)
    items_updated = models.PositiveIntegerField(default=0)
    items_skipped = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)
    message = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["source", "-started_at"], name="log_src_started_idx"),
        ]

    def __str__(self):
        return f"{self.source} · {self.status} · {self.started_at:%Y-%m-%d %H:%M}"
