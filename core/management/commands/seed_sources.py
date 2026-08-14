"""Seed the built-in source configuration (idempotent).

Usage: manage.py seed_sources
"""
from django.core.management.base import BaseCommand

from core.models import ScraperType, Source

AFRIWORK_QUERY = """
query GetJobs($limit: Int, $offset: Int, $from: timestamptz, $to: timestamptz) {
  jobs(
    limit: $limit
    offset: $offset
    # Today's listings = newly published today OR refreshed (reposted) today.
    where: {
      _or: [
        {published_at: {_gte: $from, _lt: $to}}
        {refreshed_at: {_gte: $from, _lt: $to}}
      ]
    }
    order_by: {published_at: desc}
  ) {
    id
    title
    created_at
    updated_at
    published_at
    refreshed_at
    approval_status
    description
    job_type
    job_site
    skill_requirements {
      skill {
        name
        id
      }
    }
    city {
      name
      country {
        name
      }
    }
    sectors {
      sector {
        name
        id
      }
    }
    deadline
    compensation_amount_cents
    compensation_type
    compensation_currency
    experience_level
    entity {
      type
      name
    }
  }
}
"""

AFRIWORK_HEADERS = {
    "Content-Type": "application/json",
    "x-hasura-role": "anonymous",
}

AFRIWORK_FIELD_MAPPING = {
    "external_id": "id",
    "title": "title",
    "description": {"path": "description", "transforms": ["strip_html"]},
    # The API nests location under city.name and the employer under entity.name.
    "company": "entity.name",
    "location": {"path": "city.name", "transforms": ["clean_text"]},
    "job_type": {"path": "job_type", "transforms": ["upper"]},
    "published_at": {"path": "published_at", "transforms": ["parse_datetime"]},
    "deadline": {"path": "deadline", "transforms": ["parse_datetime"]},
}

AFRIWORK_PAGINATION = {
    "page_size": 10,
    "results_path": "data.jobs",
    # "limit"/"offset" GraphQL variables are injected automatically from
    # page + page_size; only add extra variables here if the API needs them.
    # "date_filter" lets the GraphQLScraper inject today's local-day window
    # into the $from/$to variables when Source.only_today is enabled.
    "date_filter": {"field": "published_at", "from_var": "from", "to_var": "to"},
    "max_pages": 50,
}

# EthioJobs — REST GET API: api.ethiojobs.net/ethiojobs/api/job-board/jobs.
# Paginated with 1-based ?page=N&limit=M, newest-first by date_published.
# The JWT in x-custom-header is read from settings.ETHIOJOBS_TOKEN at fetch
# time (a longer-lived token can be provided via the ETHIOJOBS_TOKEN env var
# or DJANGO_SETTINGS; the seeded placeholder is replaced in production).
ETHIOJOBS_FIELD_MAPPING = {
    # Dedup key: the stable slug. The API's encrypted 'id' rotates on every
    # request, so it can never be used to identify the same job twice.
    "external_id": "slug",
    "title": "title",
    "description": {"path": "description", "transforms": ["strip_html"]},
    "company": "company.name",
    "location": {"path": "state", "transforms": ["clean_text"]},
    "job_type": {"path": "type", "transforms": ["job_type_code"]},
    "published_at": {"path": "date_published", "transforms": ["parse_datetime"]},
    "deadline": {"path": "date_expiry", "transforms": ["parse_datetime"]},
}

ETHIOJOBS_PAGINATION = {
    "page_size": 10,
    "results_path": "data",
    "page_1_based": True,  # the API numbers pages from 1
    # No from_var/to_var: the API cannot filter by date server-side, so the
    # RestJsonScraper stops the sweep client-side once a page is older than
    # today (see RestJsonScraper._past_today_boundary).
    "date_filter": {"field": "published_at"},
    "max_pages": 100,
}

# HaHuJobs — aggregator GraphQL API: graph.aggregator.hahu.jobs/v1/graphql.
# The feed mixes listings from many upstream sources (hahujobs_telegram,
# hahujobs_enterprise, addis_zemen_gazette, ethiojobs, ...); the scraper
# (core/scrapers/hahujobs.py) skips ethiojobs-sourced listings because
# EthioJobs is scraped directly. "Today" is scoped server-side on
# approved_on (when the aggregator listed the job): the $from/$to variables
# are injected by the GraphQLScraper from the date_filter below.
HAHUJOBS_QUERY = """
query GetJobs($limit: Int, $offset: Int, $from: timestamptz, $to: timestamptz) {
  jobs: search_jobs(
    where: {
      _and: [
        {expired: {_eq: false}}
        {requested_to_delete: {_eq: false}}
        {approved_on: {_gte: $from, _lt: $to}}
      ]
    }
    order_by: {approved_on: desc}
    args: {}
    offset: $offset
    limit: $limit
  ) {
    id
    title
    total_web_view_count
    telegram_view_count
    total_view_count
    type
    max_years_of_experience
    years_of_experience
    summary
    salary
    deadline
    expired
    location
    source
    application_method
    application_url
    application_email
    number_of_applicants
    approved_on
    job_cities {
      city {
        name
        region {
          name
          id
        }
      }
    }
    entity {
      logo
      name
      id
    }
    sub_sector {
      name
      sector {
        name
        id
        icon_class
        icon_code
      }
    }
    area {
      address
      name
    }
    isco_08 {
      isco_08_code
      title_en
      title_am
    }
    soc_2010 {
      title
      onetsoc_code
    }
    esco_code
  }
}
"""

HAHUJOBS_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.hahu.jobs",
    "Referer": "https://www.hahu.jobs/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}

HAHUJOBS_FIELD_MAPPING = {
    "external_id": "id",
    "title": "title",
    "description": {"path": "summary", "transforms": ["strip_html"]},
    "company": "entity.name",
    # "location" is derived in the scraper: it lives inside the job_cities
    # list (job_cities[].city.name), which dotted paths cannot reach.
    # "salary" is coerced to a string by the scraper (master column is text;
    # the per-site HaHuJob model stores it as a Decimal).
    "salary": "salary",
    "job_type": {"path": "type", "transforms": ["upper"]},
    "published_at": {"path": "approved_on", "transforms": ["parse_datetime"]},
    "deadline": {"path": "deadline", "transforms": ["parse_datetime"]},
}

HAHUJOBS_PAGINATION = {
    "page_size": 20,
    "results_path": "data.jobs",
    # Server-side today window on approved_on: the GraphQLScraper injects
    # today's local-day bounds into the $from/$to variables.
    "date_filter": {"field": "approved_on", "from_var": "from", "to_var": "to"},
    "max_pages": 50,
}

# GeezJobs — server-side HTML: https://geezjobs.com/search-jobs (paginated
# with ?page=N, ~15 cards per page, newest first). There is no JSON API; the
# GeezJobsScraper (core/scrapers/geezjobs.py) extracts the .opportunity-card
# divs. The site embeds a honeypot (.trap-field) for bots — the scraper only
# sends GETs and never submits forms. Cards only show relative 'Posted: X
# ago' timestamps, so published_at is ESTIMATED (now − offset) and the
# HtmlScraper stops the sweep client-side once a page has no items posted
# today (date_filter without from/to vars — same semantics as EthioJobs).
# The site is behind a Hostinger CDN/WAF that has been returning 403 for our
# network on every path (even in a real browser), so the source fetches
# through the free r.jina.ai relay (pagination.relay below) — see
# HtmlScraper._relay_url/fetch.
GEEZJOBS_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}

GEEZJOBS_FIELD_MAPPING = {
    "external_id": "slug",
    "title": "title",
    "company": "company",
    "location": "location",
    # The card splits employment into time + type; the master job_type gets
    # the time-based value (the only one the shared JobType enum covers). The
    # raw type (permanent/contract/...) is stored on the GeezJob detail row.
    "job_type": {"path": "job_time", "transforms": ["upper"]},
    "url": "url",
    "published_at": {"path": "published_at", "transforms": ["parse_datetime"]},
    "deadline": {"path": "deadline", "transforms": ["parse_datetime"]},
}

GEEZJOBS_PAGINATION = {
    "page_size": 15,
    # ?page=N from page 2 onward (page 1 is the bare /search-jobs URL).
    "page_1_based": True,
    "page_key": "page",
    # Fetch through the free r.jina.ai relay: HtmlScraper embeds the page URL
    # in https://r.jina.ai/<url> and asks for raw HTML (X-Return-Format: html,
    # X-No-Cache: true). GeezJobs' Hostinger WAF blocks our network (403 on
    # every path) but does not block Jina's infrastructure, so this source
    # keeps scraping while the block is in place. The relay adds latency, so
    # the timeout is raised to 60s. Set JINA_API_KEY (settings/.env) for the
    # free tier's higher request limits.
    "relay": "jina",
    "timeout": 60.0,
    # No from/to vars: the HTML feed can't be filtered by date server-side, so
    # the HtmlScraper drops pre-today items and ends the sweep once a page has
    # no items posted today (estimated from the relative posted-ago chips).
    "date_filter": {"field": "published_at"},
    "max_pages": 20,
}

# Ethiopian Reporter Jobs — WordPress (Noo Job Board theme) server-side HTML:
# https://www.ethiopianreporterjobs.com/jobs-in-ethiopia/ (path-paginated
# /page/N/, ~10 archive cards + a pinned "featured" widget per page, newest
# first). There is no JSON API; the ReporterJobsScraper
# (core/scrapers/reporterjobs.py) extracts the article.noo_job cards. Cards
# carry EXACT timestamps (<time datetime=...>) so published_at is precise.
# The newspaper posts in BATCHES (the current feed is one big August 5
# batch), so the strict today-only filter truthfully stores nothing on
# non-posting days and captures each new batch on posting days. The site is
# behind Cloudflare — a challenge page (no cards, no archive container)
# raises ScrapeError instead of silently recording an empty feed. Cloudflare
# also 403s requests from datacenter/runner IPs (GitHub Actions, cloud
# VMs) outright, so like GeezJobs the source fetches through the free
# r.jina.ai relay (pagination.relay below) — see HtmlScraper._relay_url/fetch.
REPORTER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}

REPORTER_FIELD_MAPPING = {
    # Dedup key: the stable WordPress post id (last URL segment, e.g. 284574).
    "external_id": "post_id",
    "title": "title",
    "company": "company",
    "location": "location",
    # The card's single type phrase is normalized in the scraper
    # ("Full Time" -> "full_time"); uppercased here to the shared enum.
    "job_type": {"path": "job_type", "transforms": ["upper"]},
    "url": "url",
    "published_at": {"path": "published_at", "transforms": ["parse_datetime"]},
    "deadline": {"path": "deadline", "transforms": ["parse_datetime"]},
}

REPORTER_PAGINATION = {
    "page_size": 10,
    # WordPress path pagination: page 1 is the bare /jobs-in-ethiopia/ URL,
    # page 2 is /jobs-in-ethiopia/page/2/ (page_style="path" in HtmlScraper).
    "page_1_based": True,
    "page_style": "path",
    # Fetch through the free r.jina.ai relay: Cloudflare 403s direct requests
    # from datacenter/runner IPs (GitHub Actions runners, cloud VMs) — same
    # fix that keeps GeezJobs scraping. The relay adds latency, so the timeout
    # is raised to 60s. Set JINA_API_KEY (settings/.env) for the free tier's
    # higher request limits.
    "relay": "jina",
    "timeout": 60.0,
    # No from/to vars: the HTML feed can't be filtered by date server-side, so
    # the HtmlScraper drops pre-today items and ends the sweep once a page has
    # no items posted today (exact timestamps from the <time datetime> attrs).
    "date_filter": {"field": "published_at"},
    "max_pages": 100,
}


class Command(BaseCommand):
    help = "Create or update the built-in source configurations (idempotent)."

    def handle(self, *args, **options):
        afriwork_defaults = {
            "name": "Afriwork (Freelance Ethiopia)",
            "base_url": "https://afriworket.com/jobs",
            "scraper_type": ScraperType.GRAPHQL,
            "endpoint": "https://api.afriworket.com/v1/graphql",
            "headers": AFRIWORK_HEADERS,
            "query": AFRIWORK_QUERY,
            "field_mapping": AFRIWORK_FIELD_MAPPING,
            "pagination": AFRIWORK_PAGINATION,
            "scrape_interval_hours": 24,
            "is_active": True,
        }
        afriwork, created = Source.objects.update_or_create(
            slug="afriwork", defaults=afriwork_defaults
        )
        self.stdout.write(
            self.style.SUCCESS(f"{'Created' if created else 'Updated'} source: {afriwork}")
        )

        ethiojobs_defaults = {
            "name": "EthioJobs",
            "base_url": "https://ethiojobs.net/jobs",
            "scraper_type": ScraperType.REST,
            "endpoint": "https://api.ethiojobs.net/ethiojobs/api/job-board/jobs",
            "headers": {"x-custom-header": ""},  # JWT injected from settings at fetch time
            "query": "",
            "field_mapping": ETHIOJOBS_FIELD_MAPPING,
            "pagination": ETHIOJOBS_PAGINATION,
            "scrape_interval_hours": 24,
            "is_active": True,
        }
        ethiojobs, created = Source.objects.update_or_create(
            slug="ethiojobs", defaults=ethiojobs_defaults
        )
        self.stdout.write(
            self.style.SUCCESS(f"{'Created' if created else 'Updated'} source: {ethiojobs}")
        )

        hahujobs_defaults = {
            "name": "HaHu Jobs",
            "base_url": "https://www.hahu.jobs",
            "scraper_type": ScraperType.GRAPHQL,
            "endpoint": "https://graph.aggregator.hahu.jobs/v1/graphql",
            "headers": HAHUJOBS_HEADERS,
            "query": HAHUJOBS_QUERY,
            "field_mapping": HAHUJOBS_FIELD_MAPPING,
            "pagination": HAHUJOBS_PAGINATION,
            "scrape_interval_hours": 24,
            "is_active": True,
        }
        hahujobs, created = Source.objects.update_or_create(
            slug="hahujobs", defaults=hahujobs_defaults
        )
        self.stdout.write(
            self.style.SUCCESS(f"{'Created' if created else 'Updated'} source: {hahujobs}")
        )

        geezjobs_defaults = {
            "name": "GeezJobs",
            "base_url": "https://geezjobs.com",
            "scraper_type": ScraperType.HTML,
            "endpoint": "https://geezjobs.com/search-jobs",
            "headers": GEEZJOBS_HEADERS,
            "query": "",
            "field_mapping": GEEZJOBS_FIELD_MAPPING,
            "pagination": GEEZJOBS_PAGINATION,
            "scrape_interval_hours": 24,
            "is_active": True,
        }
        geezjobs, created = Source.objects.update_or_create(
            slug="geezjobs", defaults=geezjobs_defaults
        )
        self.stdout.write(
            self.style.SUCCESS(f"{'Created' if created else 'Updated'} source: {geezjobs}")
        )

        reporterjobs_defaults = {
            "name": "Ethiopian Reporter Jobs",
            "base_url": "https://www.ethiopianreporterjobs.com",
            "scraper_type": ScraperType.HTML,
            "endpoint": "https://www.ethiopianreporterjobs.com/jobs-in-ethiopia/",
            "headers": REPORTER_HEADERS,
            "query": "",
            "field_mapping": REPORTER_FIELD_MAPPING,
            "pagination": REPORTER_PAGINATION,
            "scrape_interval_hours": 24,
            "is_active": True,
        }
        reporterjobs, created = Source.objects.update_or_create(
            slug="reporterjobs", defaults=reporterjobs_defaults
        )
        self.stdout.write(
            self.style.SUCCESS(f"{'Created' if created else 'Updated'} source: {reporterjobs}")
        )
