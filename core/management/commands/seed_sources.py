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
