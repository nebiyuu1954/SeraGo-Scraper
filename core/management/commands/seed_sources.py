"""Seed the built-in source configuration (idempotent).

Usage: manage.py seed_sources
"""
from django.core.management.base import BaseCommand

from core.models import ScraperType, Source

AFRIWORK_QUERY = """
query GetJobs($limit: Int, $offset: Int) {
  jobs(limit: $limit, offset: $offset) {
    id
    title
    description
    location
    job_type
    published_at
    created_at
    deadline
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
    "location": {"path": "location", "transforms": ["clean_text"]},
    "job_type": {"path": "job_type", "transforms": ["upper"]},
    "published_at": {"path": "published_at", "transforms": ["parse_datetime"]},
    "deadline": {"path": "deadline", "transforms": ["parse_datetime"]},
}

AFRIWORK_PAGINATION = {
    "page_size": 10,
    "results_path": "data.jobs",
    # "limit"/"offset" GraphQL variables are injected automatically from
    # page + page_size; only add extra variables here if the API needs them.
}


class Command(BaseCommand):
    help = "Create or update the built-in source configurations (idempotent)."

    def handle(self, *args, **options):
        defaults = {
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
        source, created = Source.objects.update_or_create(slug="afriwork", defaults=defaults)
        self.stdout.write(self.style.SUCCESS(f"{'Created' if created else 'Updated'} source: {source}"))
