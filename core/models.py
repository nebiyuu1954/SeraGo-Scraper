"""Core models for SeraGo: Source, ScrapedItem and the two log levels.

These tables are the contract shared with the .NET backend via PostgreSQL:
sources, scraped_items, scrape_logs (health_results comes with the Celery step).

Logs come in exactly two levels:
* ``ScrapeLog`` — the MASTER log: ONE record per day with the day's overall
  totals, a compact ``websites`` JSON (short numbers per website plus a
  reference to each website's own log), and a short ``runs`` list (one
  compact entry per overall scrape sweep).
* ``AfriworkScrapeLog`` — the per-website log: ONE record per (site, day)
  with every scrape run that day inside its ``scraped_log`` JSON — the full
  detail (pages hit, http statuses, errors) you drill into from the master.
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
    REST = "rest", "REST JSON API"
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
    only_today = models.BooleanField(
        default=True,
        help_text=(
            "Only scrape listings dated today (published or refreshed today). "
            "Requires a 'date_filter' key in pagination rules (field + "
            "from_var/to_var names); the query decides how the bounds apply."
        ),
    )
    last_scraped_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["scraper_type", "is_active"], name="src_type_active_idx"),
        ]

    def __str__(self):
        return self.name


class ScraperCreditUsage(TimeStampedModel):
    """Tracks anti-bot API credit usage per service per month.

    Each successful request through the Cloudflare rotation logs one row.
    The scraper checks remaining credits before each request; when a service
    hits 0 it is skipped for the rest of the month. Counts reset monthly
    (the ``month`` field is YYYY-MM).
    """

    SERVICE_CHOICES = [
        ("zenrows", "ZenRows"),
        ("scrapedo", "Scrape.do"),
        ("scrapebadger", "ScrapeBadger"),
        ("scrapfly", "ScrapFly"),
        ("scraperapi", "ScraperAPI"),
    ]
    SERVICE_CREDITS_PER_REQUEST = {
        "zenrows": 25,
        "scrapedo": 1,
        "scrapebadger": 2,
        "scrapfly": 50,
        "scraperapi": 25,
    }
    SERVICE_MONTHLY_FREE_CREDITS = {
        "zenrows": 5000,
        "scrapedo": 1000,
        "scrapebadger": 1000,
        "scrapfly": 1000,
        "scraperapi": 1000,
    }

    service = models.CharField(max_length=20, choices=SERVICE_CHOICES)
    credits_used = models.PositiveIntegerField(default=0)
    month = models.CharField(
        max_length=7,
        help_text="YYYY-MM — resets each calendar month",
    )
    source_slug = models.SlugField(
        max_length=120,
        blank=True,
        default="",
        help_text="Which source triggered this credit use",
    )

    class Meta:
        indexes = [
            models.Index(fields=["service", "month"], name="credit_svc_month_idx"),
        ]
        verbose_name = "Scraper credit usage"
        verbose_name_plural = "Scraper credit usage"

    def __str__(self):
        return f"{self.service} {self.month}: {self.credits_used} credits"

    @classmethod
    def remaining_credits(cls, service: str, month: str | None = None) -> int:
        """Credits still available for ``service`` in the given month."""
        if month is None:
            from django.utils import timezone
            month = timezone.localdate().strftime("%Y-%m")
        used = (
            cls.objects.filter(service=service, month=month)
            .aggregate(total=models.Sum("credits_used"))["total"]
            or 0
        )
        free = cls.SERVICE_MONTHLY_FREE_CREDITS.get(service, 0)
        return max(0, free - used)


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
        help_text="SHA-256 of title|company|location|job_type|url.",
    )
    # NOTE: the verbatim payload is NOT stored here — each per-site detail
    # row (e.g. AfriworkJob.raw_payload) keeps its own copy. The old master
    # raw_data column was removed because it duplicated that payload.
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deadline = models.DateTimeField(null=True, blank=True)
    # True when the source provided no deadline and the scraper defaulted to
    # published/first-seen + 30 days. The daily report lists these so the
    # source's deadline mapping can be fixed; the flag clears on the next
    # update once a real deadline arrives.
    deadline_is_default = models.BooleanField(default=False)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True, help_text="False once the listing disappears from the source.")
    # Per-day sequential numbering: resets to 01 for each (source, day).
    # #01 is the first job posted that day (sorted by published time); new
    # jobs found later in the day are appended with the next number.
    job_number = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Per-day sequential number (01, 02, ...), resetting each day.",
    )
    numbered_on = models.DateField(
        null=True,
        blank=True,
        help_text="Local day this item was numbered on.",
    )
    # Per-site detail records (one per site, e.g. AfriworkJob, EthioJobsJob) —
    # isolates site-specific data while ScrapedItem stays the universal
    # contract. Each website gets its own OneToOne link; only one is set.
    afriwork_job = models.OneToOneField(
        "AfriworkJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scraped_item_master",
        help_text="Afriwork-specific detail row for this listing.",
    )
    ethiojobs_job = models.OneToOneField(
        "EthioJobsJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scraped_item_master",
        help_text="EthioJobs-specific detail row for this listing.",
    )
    hahujobs_job = models.OneToOneField(
        "HaHuJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scraped_item_master",
        help_text="HaHuJobs-specific detail row for this listing.",
    )
    geezjobs_job = models.OneToOneField(
        "GeezJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scraped_item_master",
        help_text="GeezJobs-specific detail row for this listing.",
    )
    reporter_job = models.OneToOneField(
        "ReporterJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scraped_item_master",
        help_text="Ethiopian Reporter Jobs-specific detail row for this listing.",
    )

    class Meta:
        ordering = ["-numbered_on", "job_number"]
        constraints = [
            models.UniqueConstraint(fields=["source", "external_id"], name="uniq_source_external_id"),
            models.UniqueConstraint(
                fields=["source", "numbered_on", "job_number"],
                name="uniq_source_day_job_number",
            ),
        ]
        indexes = [
            models.Index(fields=["source", "is_active"], name="item_src_active_idx"),
            # SeraGo's incremental sync pulls rows with ``WHERE updated_at >
            # watermark`` on every run — without this index that query reads
            # the whole table each time (and grows with it). One index keeps
            # the watermark lookup cheap forever.
            models.Index(fields=["updated_at"], name="item_updated_at_idx"),
        ]

    @property
    def job_number_display(self) -> str:
        """Zero-padded job number, e.g. '01', '12' — or a dash when unnumbered."""
        return f"{self.job_number:02d}" if self.job_number else "—"

    def __str__(self):
        return f"{self.job_number_display} · {self.title}"


class ScrapeLog(TimeStampedModel):
    """MASTER log — ONE record per day referencing every website's logs.

    This is the single place that aggregates all websites for the day:

    * ``websites`` — ONE compact JSON: a short bucket per website
      ({source, name, table, log_id, status, run_count, api_hits, items_*}).
      ``table`` + ``log_id`` reference that website's own log (e.g. the
      AfriworkScrapeLog row) so you can jump straight to its full detail.
    * ``runs`` — ONE compact entry per overall scrape sweep ({run, time,
      hits, found, inserted, updated, skipped, status}) — the short
      'what happened this run' summary. Per-site detail (pages hit, http
      statuses, errors) stays in each website's own day log.
    * Row-level ``api_hits``/``items_*`` — the day's totals across all
      websites, plus ``websites_count`` (how many websites have logs).

    ``status`` is the WORST status across the day's runs; each website's
    worst status also sits in its bucket, so a failing website stands out
    and you can open its own log to see exactly which API call failed.
    """

    day = models.DateField(unique=True, help_text="The local day this master log covers.")
    status = models.CharField(
        max_length=16,
        choices=ScrapeStatus.choices,
        default=ScrapeStatus.SUCCESS,
        help_text="Worst status across the day's runs.",
    )
    run_count = models.PositiveIntegerField(default=0, help_text="Total scrape runs across all websites this day.")
    api_hits = models.PositiveIntegerField(default=0, help_text="Total API requests across all websites this day.")
    items_found = models.PositiveIntegerField(default=0)
    items_inserted = models.PositiveIntegerField(default=0)
    items_updated = models.PositiveIntegerField(default=0)
    items_skipped = models.PositiveIntegerField(default=0)
    websites = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Short per-website buckets: [{'source', 'name', 'table', "
            "'log_id', 'status', 'run_count', 'api_hits', 'items_*'}]. "
            "table + log_id reference the site's own log (e.g. "
            "AfriworkScrapeLog pk) where the full run detail lives."
        ),
    )
    runs = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "One compact entry per overall scrape sweep (the 2 daily "
            "scrape_all runs): {run, time, hits, found, inserted, updated, "
            "skipped, status}. Per-site day logs hold the full detail; this "
            "is the short 'what happened this run' summary."
        ),
    )

    class Meta:
        ordering = ["-day"]

    @property
    def websites_count(self) -> int:
        """How many websites have logs recorded for this day."""
        return len(self.websites or [])

    def website(self, source_slug: str) -> dict | None:
        """The per-website bucket for a source slug, if present."""
        return next((w for w in self.websites if w.get("source") == source_slug), None)

    def last_run(self) -> dict | None:
        """The most recent overall sweep entry, if any."""
        return self.runs[-1] if self.runs else None

    def __str__(self):
        return f"Master · {self.day} · {self.run_count} run(s) · {self.status}"


class AfriworkJob(TimeStampedModel):
    """Afriwork-specific job details (per-site model, isolated per website).

    Holds the fields Afriwork's GraphQL API returns, so site-specific data
    never pollutes the universal ScrapedItem contract. Linked from
    ``ScrapedItem.afriwork_job``. Other websites get their own model later
    (e.g. HaHuJob), following the same master + per-site pattern.
    """

    external_id = models.CharField(max_length=255, unique=True)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="", help_text="City name (from city.name).")
    country = models.CharField(max_length=255, blank=True, default="", help_text="Country name (from city.country.name).")
    job_type = models.CharField(max_length=32, choices=JobType.choices, blank=True, default="")
    job_site = models.CharField(max_length=32, blank=True, default="", help_text="ONSITE / REMOTE / HYBRID.")
    experience_level = models.CharField(max_length=32, blank=True, default="", help_text="ENTRY / JUNIOR / SENIOR / ...")
    approval_status = models.CharField(max_length=32, blank=True, default="", help_text="PUBLISHED / REFRESHED / ...")
    published_at = models.DateTimeField(null=True, blank=True)
    api_created_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="The API's own created_at timestamp.",
    )
    api_updated_at = models.DateTimeField(null=True, blank=True, help_text="The API's own updated_at timestamp.")
    refreshed_at = models.DateTimeField(null=True, blank=True)
    deadline = models.DateTimeField(null=True, blank=True)
    entity_type = models.CharField(max_length=32, blank=True, default="", help_text="company / private_client / ...")
    entity_name = models.CharField(max_length=255, blank=True, default="", help_text="The posting company/client name.")
    skills = models.JSONField(default=list, blank=True, help_text="Skill names, e.g. ['Canva', 'Adobe Illustrator'].")
    sectors = models.JSONField(default=list, blank=True, help_text="Sector names, e.g. ['Marketing'].")
    compensation_amount_cents = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Compensation in cents (ETB) when the API provides it.",
    )
    compensation_type = models.CharField(max_length=32, blank=True, default="", help_text="MONTHLY / FIXED / ...")
    compensation_currency = models.CharField(max_length=16, blank=True, default="", help_text="ETB / USD / ...")
    raw_payload = models.JSONField(default=dict, blank=True)
    # Same per-day numbering as the master ScrapedItem (01, 02, ...).
    job_number = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Per-day sequential number, mirroring ScrapedItem.job_number.",
    )
    numbered_on = models.DateField(
        null=True,
        blank=True,
        help_text="Local day this job was numbered on.",
    )

    class Meta:
        ordering = ["-numbered_on", "job_number"]

    @property
    def job_number_display(self) -> str:
        """Zero-padded job number, e.g. '01', '12' — or a dash when unnumbered."""
        return f"{self.job_number:02d}" if self.job_number else "—"

    def __str__(self):
        return f"{self.job_number_display} · {self.title}"


class AfriworkScrapeLog(TimeStampedModel):
    """Per-website log for Afriwork — ONE record per (source, day).

    Each scrape run that day APPENDS its summary to the ``scraped_log``
    JSON list and bumps the day totals, so a full day of scraping is
    readable and debuggable in a single row. The master ``ScrapeLog``
    references this row via its ``websites`` bucket (``log_id``).

    Example ``scraped_log`` entry (one per run that day)::

        {
            "status": "success", "page": 0,
            "items_found": 10, "items_inserted": 10,
            "items_updated": 0, "items_skipped": 0,
            "errors": [], "message": "",
            "started_at": "...", "finished_at": "...", "duration_ms": 1234,
            "api_hits": 4, "pages_hit": [{"page": 0, "http_status": 200, "found": 10}],
        }
    """

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="afriwork_day_logs")
    day = models.DateField(db_index=True, help_text="The local day this rollup covers.")
    status = models.CharField(
        max_length=16,
        choices=ScrapeStatus.choices,
        default=ScrapeStatus.SUCCESS,
        help_text="Worst status across the day's runs.",
    )
    run_count = models.PositiveIntegerField(default=0, help_text="How many scrape runs happened this day.")
    api_hits = models.PositiveIntegerField(default=0, help_text="Total API requests made this day.")
    items_found = models.PositiveIntegerField(default=0, help_text="Total items found this day.")
    items_inserted = models.PositiveIntegerField(default=0)
    items_updated = models.PositiveIntegerField(default=0)
    items_skipped = models.PositiveIntegerField(default=0)
    scraped_log = models.JSONField(
        default=list,
        blank=True,
        help_text="One summary entry per scrape run that day (grows as we scrape).",
    )

    class Meta:
        ordering = ["-day"]
        constraints = [
            models.UniqueConstraint(fields=["source", "day"], name="uniq_afriwork_daylog_src_day"),
        ]

    def last_run(self) -> dict | None:
        """The most recent run entry for this site/day."""
        return self.scraped_log[-1] if self.scraped_log else None

    def __str__(self):
        return f"Afriwork · {self.day} · {self.run_count} run(s) · {self.status}"


class EthioJobsJob(TimeStampedModel):
    """EthioJobs-specific job details (per-site model, isolated per website).

    Mirrors the EthioJobs REST API shape (``api.ethiojobs.net/ethiojobs/api/
    job-board/jobs``): every field the API returns for a listing is stored
    here so nothing is lost. ``external_id`` is the API's opaque encrypted
    job id; ``slug`` is the human-readable URL segment. Linked from
    ``ScrapedItem.ethiojobs_job``; other websites get their own model too,
    following the same master + per-site pattern.
    """

    external_id = models.CharField(max_length=500, unique=True, help_text="Dedup key — the stable job slug (the encrypted id rotates per request).")
    api_id = models.TextField(blank=True, default="", help_text="The API's opaque encrypted job id (rotates every request; kept for reference).")
    slug = models.CharField(max_length=500, blank=True, default="", help_text="URL slug, e.g. 'asJrYsRdI8-senior-international-banking-officer'.")
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True, default="")
    state = models.CharField(max_length=255, blank=True, default="", help_text="State/region, e.g. 'Addis Ababa'.")
    type = models.PositiveIntegerField(null=True, blank=True, help_text="Numeric job type code from the API.")
    level = models.CharField(max_length=32, blank=True, default="", help_text="Experience level code, e.g. '3'.")
    location_type = models.CharField(max_length=64, blank=True, default="", help_text="Office / Remote / Hybrid / ...")
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deadline = models.DateTimeField(null=True, blank=True, help_text="date_expiry from the API.")
    catalogs = models.JSONField(default=list, blank=True, help_text="[{id, name, options}] category list, e.g. Banking and Insurance.")
    company = models.JSONField(default=dict, blank=True, help_text="The full company object the API returns (name, slug, logo, ...).")
    application_method = models.CharField(max_length=64, blank=True, default="", help_text="ATS / EMAIL / CAREER_PAGE_LINK / IN_PERSON / ...")
    application_email = models.EmailField(max_length=255, blank=True, default="")
    career_page_link = models.URLField(max_length=1000, blank=True, default="")
    application_form = models.JSONField(null=True, blank=True, help_text="Embedded application form payload when present.")
    raw_payload = models.JSONField(default=dict, blank=True)
    # Same per-day numbering as the master ScrapedItem (01, 02, ...).
    job_number = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Per-day sequential number, mirroring ScrapedItem.job_number.",
    )
    numbered_on = models.DateField(
        null=True,
        blank=True,
        help_text="Local day this job was numbered on.",
    )

    class Meta:
        ordering = ["-numbered_on", "job_number"]

    @property
    def job_number_display(self) -> str:
        """Zero-padded job number, e.g. '01', '12' — or a dash when unnumbered."""
        return f"{self.job_number:02d}" if self.job_number else "—"

    def __str__(self):
        return f"{self.job_number_display} · {self.title}"


class EthioJobsScrapeLog(TimeStampedModel):
    """Per-website log for EthioJobs — ONE record per (source, day).

    Identical contract to ``AfriworkScrapeLog``: each scrape run that day
    APPENDS its summary to ``scraped_log`` and bumps the day totals. The
    master ``ScrapeLog`` references this row via its ``websites`` bucket
    (``table`` + ``log_id``).
    """

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="ethiojobs_day_logs")
    day = models.DateField(db_index=True, help_text="The local day this rollup covers.")
    status = models.CharField(
        max_length=16,
        choices=ScrapeStatus.choices,
        default=ScrapeStatus.SUCCESS,
        help_text="Worst status across the day's runs.",
    )
    run_count = models.PositiveIntegerField(default=0, help_text="How many scrape runs happened this day.")
    api_hits = models.PositiveIntegerField(default=0, help_text="Total API requests made this day.")
    items_found = models.PositiveIntegerField(default=0, help_text="Total items found this day.")
    items_inserted = models.PositiveIntegerField(default=0)
    items_updated = models.PositiveIntegerField(default=0)
    items_skipped = models.PositiveIntegerField(default=0)
    scraped_log = models.JSONField(
        default=list,
        blank=True,
        help_text="One summary entry per scrape run that day (grows as we scrape).",
    )

    class Meta:
        ordering = ["-day"]
        constraints = [
            models.UniqueConstraint(fields=["source", "day"], name="uniq_ethiojobs_daylog_src_day"),
        ]

    def last_run(self) -> dict | None:
        """The most recent run entry for this site/day."""
        return self.scraped_log[-1] if self.scraped_log else None

    def __str__(self):
        return f"EthioJobs · {self.day} · {self.run_count} run(s) · {self.status}"


class HaHuJob(TimeStampedModel):
    """HaHuJobs-specific job details (per-site model, isolated per website).

    Mirrors the HaHuJobs GraphQL API shape (``graph.aggregator.hahu.jobs``):
    every field the API returns for a listing is stored here so nothing is
    lost. ``external_id`` is the API's stable job id. Linked from
    ``ScrapedItem.hahujobs_job``. HaHuJobs is an aggregator, so ``source``
    records which upstream website a listing came from (hahujobs_telegram,
    hahujobs_enterprise, addis_zemen_gazette, ethiojobs, ...); listings
    sourced from ethiojobs are skipped by the scraper because EthioJobs is
    scraped directly.
    """

    external_id = models.CharField(max_length=255, unique=True)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True, default="", help_text="The job summary from the API.")
    type = models.CharField(
        max_length=32,
        choices=JobType.choices,
        blank=True,
        default="",
        help_text="full_time / contract / internship / ... (normalized).",
    )
    years_of_experience = models.PositiveIntegerField(null=True, blank=True)
    max_years_of_experience = models.PositiveIntegerField(null=True, blank=True)
    salary = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Monthly salary in ETB when the API provides it.",
    )
    deadline = models.DateTimeField(null=True, blank=True)
    expired = models.BooleanField(default=False)
    location = models.CharField(max_length=500, blank=True, default="", help_text="The API's free-text location string, if any.")
    source = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Aggregator source, e.g. hahujobs_telegram / hahujobs_enterprise / addis_zemen_gazette.",
    )
    application_method = models.CharField(max_length=64, blank=True, default="", help_text="link / email / in_person / hahujobs_primary / ...")
    application_url = models.URLField(max_length=1000, blank=True, default="")
    application_email = models.EmailField(max_length=255, blank=True, default="")
    number_of_applicants = models.PositiveIntegerField(null=True, blank=True)
    approved_on = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When the aggregator approved/listed the job (published_at analog).",
    )
    total_web_view_count = models.PositiveIntegerField(default=0)
    telegram_view_count = models.PositiveIntegerField(default=0)
    total_view_count = models.PositiveIntegerField(default=0)
    entity_id = models.CharField(max_length=64, blank=True, default="")
    entity_name = models.CharField(max_length=255, blank=True, default="", help_text="The posting company.")
    entity_logo = models.URLField(max_length=1000, blank=True, default="")
    sector_id = models.CharField(max_length=64, blank=True, default="")
    sector_name = models.CharField(max_length=255, blank=True, default="")
    sector_icon_class = models.CharField(max_length=64, blank=True, default="")
    sector_icon_code = models.CharField(max_length=16, blank=True, default="")
    sub_sector_name = models.CharField(max_length=255, blank=True, default="")
    area_name = models.CharField(max_length=255, blank=True, default="")
    area_address = models.CharField(max_length=500, blank=True, default="")
    isco_08_code = models.CharField(max_length=16, blank=True, default="")
    isco_08_title_en = models.CharField(max_length=500, blank=True, default="")
    isco_08_title_am = models.CharField(max_length=500, blank=True, default="")
    soc_2010_title = models.CharField(max_length=500, blank=True, default="")
    soc_2010_onetsoc_code = models.CharField(max_length=32, blank=True, default="")
    esco_code = models.CharField(max_length=32, blank=True, default="")
    cities = models.JSONField(default=list, blank=True, help_text="City names, e.g. ['Addis Ababa'].")
    raw_payload = models.JSONField(default=dict, blank=True)
    # Same per-day numbering as the master ScrapedItem (01, 02, ...).
    job_number = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Per-day sequential number, mirroring ScrapedItem.job_number.",
    )
    numbered_on = models.DateField(
        null=True,
        blank=True,
        help_text="Local day this job was numbered on.",
    )

    class Meta:
        ordering = ["-numbered_on", "job_number"]

    @property
    def job_number_display(self) -> str:
        """Zero-padded job number, e.g. '01', '12' — or a dash when unnumbered."""
        return f"{self.job_number:02d}" if self.job_number else "—"

    @property
    def cities_display(self) -> str:
        """Comma-joined city names for admin lists, e.g. 'Addis Ababa'."""
        return ", ".join(self.cities or [])

    def __str__(self):
        return f"{self.job_number_display} · {self.title}"


class HaHuScrapeLog(TimeStampedModel):
    """Per-website log for HaHuJobs — ONE record per (source, day).

    Identical contract to ``AfriworkScrapeLog``: each scrape run that day
    APPENDS its summary to ``scraped_log`` and bumps the day totals. The
    master ``ScrapeLog`` references this row via its ``websites`` bucket
    (``table`` + ``log_id``).
    """

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="hahujobs_day_logs")
    day = models.DateField(db_index=True, help_text="The local day this rollup covers.")
    status = models.CharField(
        max_length=16,
        choices=ScrapeStatus.choices,
        default=ScrapeStatus.SUCCESS,
        help_text="Worst status across the day's runs.",
    )
    run_count = models.PositiveIntegerField(default=0, help_text="How many scrape runs happened this day.")
    api_hits = models.PositiveIntegerField(default=0, help_text="Total API requests made this day.")
    items_found = models.PositiveIntegerField(default=0, help_text="Total items found this day.")
    items_inserted = models.PositiveIntegerField(default=0)
    items_updated = models.PositiveIntegerField(default=0)
    items_skipped = models.PositiveIntegerField(default=0)
    scraped_log = models.JSONField(
        default=list,
        blank=True,
        help_text="One summary entry per scrape run that day (grows as we scrape).",
    )

    class Meta:
        ordering = ["-day"]
        constraints = [
            models.UniqueConstraint(fields=["source", "day"], name="uniq_hahujobs_daylog_src_day"),
        ]

    def last_run(self) -> dict | None:
        """The most recent run entry for this site/day."""
        return self.scraped_log[-1] if self.scraped_log else None

    def __str__(self):
        return f"HaHuJobs · {self.day} · {self.run_count} run(s) · {self.status}"


class GeezJob(TimeStampedModel):
    """GeezJobs-specific job details (per-site model, isolated per website).

    GeezJobs (``geezjobs.com``) is a server-side HTML site — there is no JSON
    API — so the per-site row mirrors the fields shown on each listing card on
    ``/search-jobs`` (title, company, location, deadline, employment,
    experience, ``Posted: X ago`` chip, logo). ``external_id`` is the stable
    job-detail slug (the last segment of the detail URL, e.g.
    ``office-engineer-4b-trading-plc``).

    ``published_at`` is ESTIMATED from the card's relative ``Posted: X ago``
    chip — the site exposes no exact timestamps — and drives the today-only
    filter and per-day numbering. The site splits employment into a job TIME
    (``full_time`` / ``part_time``) and a job TYPE (``permanent`` / ``contract`` /
    ``internship`` / ``freelance`` / ``volunteer``); both raw values are kept here
    (the shared ``JobType`` enum has no ``permanent``/``volunteer`` values),
    while the master ``ScrapedItem.job_type`` carries the time-based value.
    Linked from ``ScrapedItem.geezjobs_job``.
    """

    external_id = models.CharField(
        max_length=255,
        unique=True,
        help_text="Dedup key — the job-detail slug from the listing URL.",
    )
    title = models.CharField(max_length=500)
    slug = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="URL slug, e.g. 'office-engineer-4b-trading-plc'.",
    )
    company = models.CharField(max_length=255, blank=True, default="")
    company_logo = models.URLField(
        max_length=1000,
        blank=True,
        default="",
        help_text="Company logo URL from the card (cards without a logo show a letter placeholder).",
    )
    location = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="City/area from the card, e.g. 'Addis Ababa' or 'Kality, Addis Ababa'.",
    )
    country = models.CharField(max_length=64, blank=True, default="", help_text="Country shown on the card (usually 'Ethiopia').")
    deadline = models.DateTimeField(null=True, blank=True, help_text="Parsed from the card's deadline text.")
    deadline_text = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Raw card text, e.g. 'Deadline: September 7, 2026'.",
    )
    employment_text = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Raw chip, e.g. 'Full-time / Permanent'.",
    )
    job_time = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Site's job-time filter value: full_time / part_time.",
    )
    job_type = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Site's job-type filter value: permanent / contract / internship / freelance / volunteer.",
    )
    experience_text = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Raw chip, e.g. '3+ Years' or '2/3 Years'.",
    )
    min_experience_years = models.PositiveIntegerField(null=True, blank=True)
    max_experience_years = models.PositiveIntegerField(null=True, blank=True)
    posted_text = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Raw chip, e.g. 'Posted: 3 min ago'.",
    )
    published_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Estimated from the 'Posted: X ago' chip (the site exposes no exact timestamps).",
    )
    url = models.URLField(max_length=1000, blank=True, default="", help_text="Absolute job-detail URL.")
    raw_payload = models.JSONField(default=dict, blank=True, help_text="The parsed card fields (raw HTML-derived data).")
    # Same per-day numbering as the master ScrapedItem (01, 02, ...).
    job_number = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Per-day sequential number, mirroring ScrapedItem.job_number.",
    )
    numbered_on = models.DateField(
        null=True,
        blank=True,
        help_text="Local day this job was numbered on.",
    )

    class Meta:
        ordering = ["-numbered_on", "job_number"]

    @property
    def job_number_display(self) -> str:
        """Zero-padded job number, e.g. '01', '12' — or a dash when unnumbered."""
        return f"{self.job_number:02d}" if self.job_number else "—"

    @property
    def employment_display(self) -> str:
        """Joined employment values for admin lists, e.g. 'full_time / permanent'."""
        return " / ".join(part for part in (self.job_time, self.job_type) if part) or "—"

    def __str__(self):
        return f"{self.job_number_display} · {self.title}"


class GeezScrapeLog(TimeStampedModel):
    """Per-website log for GeezJobs — ONE record per (source, day).

    Identical contract to ``AfriworkScrapeLog``: each scrape run that day
    APPENDS its summary to ``scraped_log`` and bumps the day totals. The
    master ``ScrapeLog`` references this row via its ``websites`` bucket
    (``table`` + ``log_id``).
    """

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="geezjobs_day_logs")
    day = models.DateField(db_index=True, help_text="The local day this rollup covers.")
    status = models.CharField(
        max_length=16,
        choices=ScrapeStatus.choices,
        default=ScrapeStatus.SUCCESS,
        help_text="Worst status across the day's runs.",
    )
    run_count = models.PositiveIntegerField(default=0, help_text="How many scrape runs happened this day.")
    api_hits = models.PositiveIntegerField(default=0, help_text="Total API requests made this day.")
    items_found = models.PositiveIntegerField(default=0, help_text="Total items found this day.")
    items_inserted = models.PositiveIntegerField(default=0)
    items_updated = models.PositiveIntegerField(default=0)
    items_skipped = models.PositiveIntegerField(default=0)
    scraped_log = models.JSONField(
        default=list,
        blank=True,
        help_text="One summary entry per scrape run that day (grows as we scrape).",
    )

    class Meta:
        ordering = ["-day"]
        constraints = [
            models.UniqueConstraint(fields=["source", "day"], name="uniq_geezjobs_daylog_src_day"),
        ]

    def last_run(self) -> dict | None:
        """The most recent run entry for this site/day."""
        return self.scraped_log[-1] if self.scraped_log else None

    def __str__(self):
        return f"GeezJobs · {self.day} · {self.run_count} run(s) · {self.status}"


class ReporterJob(TimeStampedModel):
    """Ethiopian Reporter Jobs-specific job details (per-site model, isolated per website).

    Ethiopian Reporter Jobs (``www.ethiopianreporterjobs.com``) is a WordPress
    job board (Careerfy theme, mid-2026 redesign) — there is no JSON API — so
    the per-site row mirrors each ``div.jobsearch-joblisting-classic-wrap``
    card on ``/jobs-in-ethiopia/`` (title, company, job type, location,
    posted time). ``external_id`` is the stable WordPress post id (the
    ``data-job-id`` attribute / last segment of the detail URL, e.g.
    ``284574``).

    The cards only expose a RELATIVE posting time ("Published X hours ago"),
    so ``published_at`` is estimated (like GeezJobs) and drives the
    today-only filter and per-day numbering; the theme has the deadline field
    disabled, so ``deadline`` is left unset and the shared +30-day default
    applies (flagged ``deadline_is_default``). The job type badge is a single
    phrase ("Full-time" / "Contract" / ...): the raw text is kept here
    (``job_type_text``) alongside the site-normalized value (``job_type``),
    while the master ``ScrapedItem.job_type`` carries the shared enum value.
    Linked from ``ScrapedItem.reporter_job``.
    """

    external_id = models.CharField(
        max_length=255,
        unique=True,
        help_text="Dedup key — the WordPress post id from the listing URL.",
    )
    title = models.CharField(max_length=500)
    url = models.URLField(max_length=1000, blank=True, default="", help_text="Absolute job-detail URL.")
    company = models.CharField(max_length=255, blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="", help_text="City/area from the card, e.g. 'Addis Ababa'.")
    job_type_text = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Raw card chip, e.g. 'Full Time'.",
    )
    job_type = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Site's normalized job-type value: full_time / part_time / contract / internship / freelance / temporary / ...",
    )
    posted_text = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Raw card date text, e.g. 'August 5, 2026'.",
    )
    published_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Exact timestamp from the card's <time datetime> attribute.",
    )
    deadline_text = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Raw closing date text, e.g. 'August 12, 2026'.",
    )
    deadline = models.DateTimeField(null=True, blank=True, help_text="Parsed from the card's closing date.")
    raw_payload = models.JSONField(default=dict, blank=True, help_text="The parsed card fields (raw HTML-derived data).")
    # Same per-day numbering as the master ScrapedItem (01, 02, ...).
    job_number = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Per-day sequential number, mirroring ScrapedItem.job_number.",
    )
    numbered_on = models.DateField(
        null=True,
        blank=True,
        help_text="Local day this job was numbered on.",
    )

    class Meta:
        ordering = ["-numbered_on", "job_number"]

    @property
    def job_number_display(self) -> str:
        """Zero-padded job number, e.g. '01', '12' — or a dash when unnumbered."""
        return f"{self.job_number:02d}" if self.job_number else "—"

    @property
    def job_type_display(self) -> str:
        """Human-readable type: raw chip when present, else the normalized value."""
        return self.job_type_text or self.job_type or "—"

    def __str__(self):
        return f"{self.job_number_display} · {self.title}"


class ReporterScrapeLog(TimeStampedModel):
    """Per-website log for Ethiopian Reporter Jobs — ONE record per (source, day).

    Identical contract to ``AfriworkScrapeLog``: each scrape run that day
    APPENDS its summary to ``scraped_log`` and bumps the day totals. The
    master ``ScrapeLog`` references this row via its ``websites`` bucket
    (``table`` + ``log_id``).
    """

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="reporterjobs_day_logs")
    day = models.DateField(db_index=True, help_text="The local day this rollup covers.")
    status = models.CharField(
        max_length=16,
        choices=ScrapeStatus.choices,
        default=ScrapeStatus.SUCCESS,
        help_text="Worst status across the day's runs.",
    )
    run_count = models.PositiveIntegerField(default=0, help_text="How many scrape runs happened this day.")
    api_hits = models.PositiveIntegerField(default=0, help_text="Total API requests made this day.")
    items_found = models.PositiveIntegerField(default=0, help_text="Total items found this day.")
    items_inserted = models.PositiveIntegerField(default=0)
    items_updated = models.PositiveIntegerField(default=0)
    items_skipped = models.PositiveIntegerField(default=0)
    scraped_log = models.JSONField(
        default=list,
        blank=True,
        help_text="One summary entry per scrape run that day (grows as we scrape).",
    )

    class Meta:
        ordering = ["-day"]
        constraints = [
            models.UniqueConstraint(fields=["source", "day"], name="uniq_reporterjobs_daylog_src_day"),
        ]

    def last_run(self) -> dict | None:
        """The most recent run entry for this site/day."""
        return self.scraped_log[-1] if self.scraped_log else None

    def __str__(self):
        return f"Reporter Jobs · {self.day} · {self.run_count} run(s) · {self.status}"


class ArchiveRun(TimeStampedModel):
    """One row per successful weekly archive send — the 'sent' note.

    The Sunday archive writes a row here only AFTER both weekly files
    uploaded to Telegram successfully. Monday's cleanup checks it: if a row
    exists for the Sunday just past, Sunday's kept log rows may be deleted;
    if not, the Sunday archive failed and Monday retries it. This note is
    what guarantees a Monday cleanup can never delete data that was never
    filed.

    ``archived_on`` is the calendar day the files cover (the Sunday of the
    week just ended — ``update_or_create`` keeps one row per Sunday, so a
    Monday retry overwrites the failed Sunday's entry).
    """

    archived_on = models.DateField(
        unique=True,
        help_text="The Sunday the archived week ended (the files cover Mon–Sun up to this day).",
    )
    jobs_file = models.CharField(max_length=255, blank=True, default="", help_text="Sent jobs filename, e.g. jobs-2026-08-16.jsonl.gz.")
    logs_file = models.CharField(max_length=255, blank=True, default="", help_text="Sent logs filename, e.g. logs-2026-08-16.json.gz.")
    jobs_count = models.PositiveIntegerField(default=0, help_text="How many ended jobs the jobs file contained.")
    log_rows = models.PositiveIntegerField(default=0, help_text="How many master + per-site log rows the logs file contained.")
    sent_at = models.DateTimeField(auto_now_add=True, help_text="When both files finished uploading.")

    class Meta:
        ordering = ["-archived_on"]

    def __str__(self):
        return f"Archive through {self.archived_on} · {self.jobs_count} jobs · {self.log_rows} log rows"


class ScrapeStat(TimeStampedModel):
    """PERSISTENT weekly/monthly rollups of the scrape logs.

    The day logs (master + per-site) are archived to Telegram files and
    deleted weekly, so their history lives only in files. This table is the
    permanent record instead: one row per calendar week (Mon–Sun) and one
    per calendar month, holding the same day-level aggregates the logs carry
    (runs, api hits, items found/inserted/updated/skipped, per-source
    breakdown) plus a failure summary. Rows are NEVER deleted — recomputed
    (upserted) after every scrape and again right before the archive deletes
    the logs, so the numbers are final even though the underlying logs are
    gone. Size is negligible (~52 week + 12 month rows a year).
    """

    class PeriodType(models.TextChoices):
        DAY = "day", "Day"
        WEEK = "week", "Week"
        MONTH = "month", "Month"
        YEAR = "year", "Year"

    period_type = models.CharField(max_length=8, choices=PeriodType.choices)
    period_start = models.DateField(help_text="Monday for a week; the 1st for a month.")
    period_end = models.DateField(help_text="Sunday for a week; the last day for a month.")
    days_with_runs = models.PositiveIntegerField(default=0, help_text="How many days in the period recorded logs.")
    run_count = models.PositiveIntegerField(default=0)
    api_hits = models.PositiveIntegerField(default=0)
    items_found = models.PositiveIntegerField(default=0)
    items_inserted = models.PositiveIntegerField(default=0)
    items_updated = models.PositiveIntegerField(default=0)
    items_skipped = models.PositiveIntegerField(default=0)
    runs_by_status = models.JSONField(
        default=dict,
        blank=True,
        help_text='{"success": N, "partial": N, "failed": N} across all per-site runs in the period.',
    )
    top_errors = models.JSONField(
        default=list,
        blank=True,
        help_text='Most common run error messages: [{"message": ..., "count": N}] (top 5).',
    )
    by_source = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-source aggregates: {slug: {run_count, api_hits, items_*, days_with_runs, failed_runs}}.",
    )

    class Meta:
        ordering = ["-period_start"]
        constraints = [
            models.UniqueConstraint(
                fields=["period_type", "period_start"],
                name="uniq_scrapestat_type_start",
            ),
        ]

    def __str__(self):
        return f"{self.period_type} {self.period_start} → {self.period_end} · {self.run_count} runs"


class CategoryStat(TimeStampedModel):
    """PERSISTENT daily counts of a normalized category (sectors today).

    The top-sectors breakdown the SeraGo dashboard shows. Kept at DAY
    granularity on purpose: a day's detail rows exist until the weekly
    archive prunes them, so the count for each day is computed (and upserted)
    while that day is current, and week/month/year figures are derived by
    summing these rows — history survives even though the underlying job
    rows are deleted. Rows are NEVER deleted (~30-60 sector names x 365 days
    a year — negligible).

    ``category_type`` is the extensible dimension: "sector" today; future
    normalized dimensions (e.g. "job" / "company") add their own rows
    without a schema change.
    """

    category_type = models.CharField(
        max_length=16,
        help_text="The normalized dimension, e.g. 'sector' (future: 'job', ...).",
    )
    period_start = models.DateField(
        db_index=True, help_text="The local day these counts were computed for."
    )
    category_name = models.CharField(max_length=255, help_text="Sector (or category) name.")
    count = models.PositiveIntegerField(default=0, help_text="Jobs in this category found that day.")

    class Meta:
        ordering = ["-period_start", "-count"]
        constraints = [
            models.UniqueConstraint(
                fields=["category_type", "period_start", "category_name"],
                name="uniq_categorystat_type_day_name",
            ),
        ]
        indexes = [
            # The SeraGo stats API sums rows ``WHERE period_start BETWEEN …``
            # per category_type — one index keeps that aggregation cheap.
            models.Index(fields=["category_type", "period_start"], name="catstat_type_day_idx"),
        ]

    def __str__(self):
        return f"{self.category_type} {self.period_start} · {self.category_name} ×{self.count}"


# Registry of per-website log models (ONE record per source+day each). The
# master ``ScrapeLog`` references each website's own log row (``table`` +
# ``log_id``) so you can drill from the summary into the full detail.
# Append new website logs here as they are built (e.g. ``HaHuScrapeLog``)
# and the master rollup picks them up automatically.
SITE_LOG_MODELS = [
    AfriworkScrapeLog,
    EthioJobsScrapeLog,
    HaHuScrapeLog,
    GeezScrapeLog,
    ReporterScrapeLog,
]