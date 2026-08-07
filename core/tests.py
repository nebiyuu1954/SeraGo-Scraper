"""Unit tests for the scraper pipeline — no network calls (fetch is stubbed)."""
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase

from core.models import ScrapeStatus, ScrapedItem, ScraperType, Source
from core.scrapers import GraphQLScraper, ScraperFactory, ScrapeError
from core.scrapers.base import transform_strip_html


def make_source() -> Source:
    return Source.objects.create(
        name="Afriwork (test)",
        slug="afriwork-test",
        base_url="https://afriworket.com/jobs",
        scraper_type=ScraperType.GRAPHQL,
        endpoint="https://api.afriworket.com/v1/graphql",
        headers={"Content-Type": "application/json", "x-hasura-role": "anonymous"},
        query="query GetJobs($limit: Int, $offset: Int) { jobs(limit: $limit, offset: $offset) { id title } }",
        field_mapping={
            "external_id": "id",
            "title": "title",
            "description": {"path": "description", "transforms": ["strip_html"]},
            "location": {"path": "location", "transforms": ["clean_text"]},
            "job_type": {"path": "job_type", "transforms": ["upper"]},
            "published_at": {"path": "published_at", "transforms": ["parse_datetime"]},
        },
        pagination={"page_size": 10, "results_path": "data.jobs"},
    )


class FakeGraphQLScraper(GraphQLScraper):
    """GraphQLScraper with ``fetch`` stubbed to a canned payload."""

    def __init__(self, source: Source, payload: dict):
        super().__init__(source)
        self._payload = payload

    def fetch(self, page: int = 0) -> dict:
        return self._payload


class FieldMappingTests(TestCase):
    def setUp(self):
        self.scraper = FakeGraphQLScraper(make_source(), {})

    def test_dotted_paths_and_transforms(self):
        normalized = self.scraper.normalize(
            {
                "id": "abc-123",
                "title": "Driver",
                "description": "<p>Hello <b>world</b></p>",
                "location": "  Addis Ababa  ",
                "job_type": "part_time",
                "published_at": "2026-06-12T08:10:14+00:00",
            }
        )
        self.assertEqual(normalized["external_id"], "abc-123")
        self.assertEqual(normalized["title"], "Driver")
        self.assertEqual(normalized["description"], "Hello world")
        self.assertEqual(normalized["location"], "Addis Ababa")
        self.assertEqual(normalized["job_type"], "PART_TIME")
        self.assertIsNotNone(normalized["published_at"])
        self.assertEqual(normalized["published_at"].tzinfo is not None, True)

    def test_strip_html_transform_directly(self):
        self.assertEqual(transform_strip_html("<p>A<br/>B</p>"), "A B")
        self.assertIsNone(transform_strip_html(None))
        self.assertIsNone(transform_strip_html("  <p>  </p>  "))

    def test_unmapped_fields_are_ignored(self):
        normalized = self.scraper.normalize({"id": "x", "title": "T", "unmapped": "junk"})
        self.assertNotIn("unmapped", normalized)


class PipelineTests(TestCase):
    PAYLOAD = {
        "data": {
            "jobs": [
                {
                    "id": "j1",
                    "title": "Driver",
                    "description": "<p>Drive safely</p>",
                    "location": "Addis Ababa",
                    "job_type": "full_time",
                    "published_at": "2026-06-12T08:10:14+00:00",
                },
                {
                    "id": "j2",
                    "title": "Cook",
                    "description": "Cook food",
                    "location": None,
                    "job_type": "part_time",
                    "published_at": None,
                },
            ]
        }
    }

    def setUp(self):
        self.source = make_source()
        self.scraper = FakeGraphQLScraper(self.source, self.PAYLOAD)

    def test_scrape_creates_items_and_log(self):
        log = self.scraper.scrape()

        self.assertEqual(log.status, ScrapeStatus.SUCCESS)
        self.assertEqual(log.items_found, 2)
        self.assertEqual(log.items_inserted, 2)
        self.assertEqual(ScrapedItem.objects.count(), 2)

        item = ScrapedItem.objects.get(external_id="j1")
        self.assertEqual(item.title, "Driver")
        self.assertEqual(item.description, "Drive safely")
        self.assertEqual(item.job_type, "FULL_TIME")
        self.assertEqual(item.source, self.source)
        self.assertEqual(len(item.content_hash), 64)
        self.assertEqual(item.raw_data["id"], "j1")

        # Source timestamps updated on success.
        self.source.refresh_from_db()
        self.assertIsNotNone(self.source.last_scraped_at)
        self.assertIsNotNone(self.source.last_success_at)

    def test_second_run_deduplicates(self):
        self.scraper.scrape()
        log2 = self.scraper.scrape()

        self.assertEqual(log2.status, ScrapeStatus.SUCCESS)
        self.assertEqual(log2.items_found, 2)
        self.assertEqual(log2.items_inserted, 0)
        self.assertEqual(log2.items_skipped, 2)
        self.assertEqual(ScrapedItem.objects.count(), 2)

    def test_changed_item_is_updated(self):
        self.scraper.scrape()

        # Same external_id, different title -> new content hash.
        payload = {
            "data": {
                "jobs": [
                    {
                        "id": "j1",
                        "title": "Senior Driver",
                        "description": "<p>Drive safely</p>",
                        "location": "Addis Ababa",
                        "job_type": "full_time",
                        "published_at": "2026-06-12T08:10:14+00:00",
                    }
                ]
            }
        }
        log = FakeGraphQLScraper(self.source, payload).scrape()

        self.assertEqual(log.items_updated, 1)
        item = ScrapedItem.objects.get(external_id="j1")
        self.assertEqual(item.title, "Senior Driver")

    def test_bad_payload_raises_and_marks_log_failed(self):
        bad = FakeGraphQLScraper(self.source, {"data": {}})
        with self.assertRaises(ScrapeError):
            bad.scrape()
        failed_log = self.source.logs.get()
        self.assertEqual(failed_log.status, ScrapeStatus.FAILED)
        self.assertTrue(failed_log.errors)

    def test_insert_race_with_concurrent_worker_is_not_an_error(self):
        """IntegrityError on insert is treated as a lost race, not an item error."""
        payload = {
            "data": {
                "jobs": [
                    {
                        "id": "race-item",
                        "title": "Racer",
                        "description": "x",
                        "location": None,
                        "job_type": "full_time",
                        "published_at": None,
                    }
                ]
            }
        }
        scraper = FakeGraphQLScraper(self.source, payload)
        with patch.object(
            ScrapedItem.objects, "create", side_effect=IntegrityError("duplicate key")
        ):
            log = scraper.scrape()

        self.assertEqual(log.status, ScrapeStatus.SUCCESS)
        self.assertEqual(log.items_inserted, 0)
        self.assertEqual(log.items_skipped, 1)
        self.assertEqual(log.errors, [])
        self.assertEqual(ScrapedItem.objects.filter(external_id="race-item").count(), 0)

    def test_missing_external_id_is_per_item_error(self):
        payload = {
            "data": {
                "jobs": [
                    {"id": "ok", "title": "Good"},
                    {"id": None, "title": "Bad"},
                ]
            }
        }
        log = FakeGraphQLScraper(self.source, payload).scrape()

        self.assertEqual(log.status, ScrapeStatus.PARTIAL)
        self.assertEqual(log.items_inserted, 1)
        self.assertEqual(len(log.errors), 1)
        self.assertEqual(ScrapedItem.objects.count(), 1)


class PaginationTests(TestCase):
    def test_payload_advances_offset_per_page(self):
        source = make_source()
        scraper = GraphQLScraper(source)
        self.assertEqual(scraper._build_payload(0)["variables"], {"limit": 10, "offset": 0})
        self.assertEqual(scraper._build_payload(1)["variables"], {"limit": 10, "offset": 10})
        self.assertEqual(scraper._build_payload(2)["variables"], {"limit": 10, "offset": 20})

    def test_custom_limit_offset_variable_names(self):
        source = make_source()
        source.pagination = {"page_size": 5, "limit_var": "first", "offset_var": "skip"}
        source.save()
        scraper = GraphQLScraper(source)
        self.assertEqual(scraper._build_payload(2)["variables"], {"first": 5, "skip": 10})


class FactoryTests(TestCase):
    def test_factory_returns_graphql_scraper(self):
        scraper = ScraperFactory.for_source(make_source())
        self.assertIsInstance(scraper, GraphQLScraper)
