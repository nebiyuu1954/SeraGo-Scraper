"""Tests for SeraGo — enough to manage multiple websites.

Covers the three things that matter when running several scrapers:
1. Structure change detection — each website's API response shape is snapshotted
   (``core/structure_snapshots/{slug}.json``); if a website changes its API the
   snapshot check fails and we know about it.
2. API status checks — every response of the day is logged (``pages_hit`` with
   ``http_status``); a helper + the ``log_report`` command surface any non-200.
3. The day-log rollups (master + per-website) stay correct and lean.
"""
import io
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.models import AfriworkScrapeLog, ScrapeLog, ScrapeStatus, Source
from core.reporting import api_issues_for_day
from core.scrapers.graphql import GraphQLScraper
from core.structures import (
    compare_structures,
    extract_structure,
    load_structure,
    snapshot_path,
)

# A faithful sample of ONE Afriwork job object. Its flattened field paths must
# stay a subset of the live-captured structure snapshot (afriwork.json), so
# this sample mirrors the API shape exactly (no __typename etc. unless the
# query returns them).
AFRIWORK_SAMPLE = {
    "id": "5974efaa-bc21-4f1e-bdb2-51b8c3c45544",
    "title": "Junior Multimedia Designer",
    "created_at": "2026-08-07T08:53:08.799033+00:00",
    "updated_at": "2026-08-07T08:53:08.950584+00:00",
    "published_at": "2026-08-07T08:53:08+00:00",
    "refreshed_at": None,
    "approval_status": "PUBLISHED",
    "description": "<p>Some job description</p>",
    "job_type": "FULL_TIME",
    "job_site": "ONSITE",
    "skill_requirements": [
        {"skill": {"name": "Canva", "id": "66e7a42d-13c4-4929-8a01-51a0cad54ab0"}}
    ],
    "city": {"name": "Addis Ababa", "country": {"name": "ETHIOPIA"}},
    "sectors": [
        {"sector": {"name": "Marketing", "id": "44f0aead-07c8-4c1b-a28c-58e7e0f60df6"}}
    ],
    "deadline": "2026-08-13T00:00:00+00:00",
    "compensation_amount_cents": None,
    "compensation_type": "MONTHLY",
    "compensation_currency": None,
    "experience_level": "JUNIOR",
    "entity": {"type": "company", "name": "Dodai Manufacturing PLC"},
}

AFRIWORK_SAMPLE_PATHS = [
    "approval_status",
    "city.country.name",
    "city.name",
    "compensation_amount_cents",
    "compensation_currency",
    "compensation_type",
    "created_at",
    "deadline",
    "description",
    "entity.name",
    "entity.type",
    "experience_level",
    "id",
    "job_site",
    "job_type",
    "published_at",
    "refreshed_at",
    "sectors.sector.id",
    "sectors.sector.name",
    "skill_requirements.skill.id",
    "skill_requirements.skill.name",
    "title",
    "updated_at",
]


def make_run(
    status="success",
    api_hits=1,
    found=3,
    http_status=200,
    errors=None,
    message="",
):
    """A run-summary dict in the same shape the scrapers write to the logs."""
    return {
        "status": status,
        "page": 0,
        "items_found": found,
        "items_inserted": found if status == "success" else 0,
        "items_updated": 0,
        "items_skipped": 0,
        "errors": errors or [],
        "message": message,
        "started_at": "2026-08-07T10:00:00+00:00",
        "finished_at": "2026-08-07T10:00:01+00:00",
        "duration_ms": 500,
        "api_hits": api_hits,
        "pages_hit": [{"page": 0, "http_status": http_status, "found": found}],
    }


class StructureTests(TestCase):
    """The website structure snapshot: extract, store, compare, detect change."""

    def test_extract_structure_flattens_nested_and_lists(self):
        self.assertEqual(extract_structure(AFRIWORK_SAMPLE), AFRIWORK_SAMPLE_PATHS)

    def test_extract_structure_empty_payload(self):
        self.assertEqual(extract_structure({}), [])
        self.assertEqual(extract_structure(None), [])

    def test_afriwork_snapshot_exists_and_is_valid(self):
        snapshot = load_structure("afriwork")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["source"], "afriwork")
        self.assertTrue(snapshot["fields"])

    def test_snapshot_contains_core_structure(self):
        # The stored snapshot must still contain every field we rely on. If the
        # live API drops one, re-capturing removes it from the snapshot and
        # this test fails — that's the structure-change alarm.
        snapshot = load_structure("afriwork")
        self.assertIsNotNone(snapshot)
        missing = set(AFRIWORK_SAMPLE_PATHS) - set(snapshot["fields"])
        self.assertFalse(missing, f"Snapshot is missing core fields: {sorted(missing)}")

    def test_compare_structures_reports_diff(self):
        added, removed = compare_structures(["a", "b", "c"], ["a", "b", "d"])
        self.assertEqual(added, ["c"])
        self.assertEqual(removed, ["d"])

    def test_capture_structure_writes_snapshot(self):
        class StubScraper:
            def fetch(self, page=0):
                return {}

            def parse(self, raw):
                return [AFRIWORK_SAMPLE]

        source = Source.objects.create(
            slug="afriwork",
            name="Afriwork",
            endpoint="https://example.com/graphql",
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "afriwork.json"
            with mock.patch(
                "core.management.commands.capture_structure.snapshot_path",
                return_value=target,
            ), mock.patch(
                "core.management.commands.capture_structure.ScraperFactory"
            ) as factory:
                factory.for_source.return_value = StubScraper()
                call_command("capture_structure", "afriwork", stdout=io.StringIO())
            self.assertTrue(target.exists())
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"], "afriwork")
            self.assertEqual(payload["fields"], AFRIWORK_SAMPLE_PATHS)


class ApiIssueTests(TestCase):
    """Every API response of the day must be visible; non-200s must be caught."""

    def setUp(self):
        self.source = Source.objects.create(
            slug="afriwork",
            name="Afriwork",
            endpoint="https://example.com/graphql",
        )
        self.today = timezone.localdate()

    def _day_logs(self, runs, site_status=None, master_status=None):
        """Create the master + per-site day log rows for a list of runs."""
        site_status = site_status or runs[-1]["status"]
        master_status = master_status or site_status
        master = ScrapeLog.objects.create(
            day=self.today,
            status=master_status,
            run_count=len(runs),
            api_hits=sum(r.get("api_hits", 0) for r in runs),
            items_found=sum(r.get("items_found", 0) for r in runs),
            items_inserted=sum(r.get("items_inserted", 0) for r in runs),
            items_updated=0,
            items_skipped=0,
        )
        site = AfriworkScrapeLog.objects.create(
            source=self.source,
            day=self.today,
            status=site_status,
            run_count=len(runs),
            api_hits=master.api_hits,
            items_found=master.items_found,
            items_inserted=master.items_inserted,
            items_updated=0,
            items_skipped=0,
        )
        site.scraped_log = runs
        site.save()
        return master, site

    def test_no_issues_when_every_response_is_200(self):
        self._day_logs([make_run(http_status=200)])
        self.assertEqual(api_issues_for_day(self.today), [])

    def test_non_200_response_is_reported(self):
        run = make_run(api_hits=2, found=3)
        run["pages_hit"] = [
            {"page": 0, "http_status": 200, "found": 3},
            {"page": 1, "http_status": 503, "found": 0},
        ]
        self._day_logs([run])
        issues = api_issues_for_day(self.today)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["kind"], "http")
        self.assertEqual(issues[0]["website"], "afriwork")
        self.assertEqual(issues[0]["page"], 1)
        self.assertEqual(issues[0]["http_status"], 503)
        self.assertEqual(issues[0]["table"], "AfriworkScrapeLog")

    def test_failed_run_is_reported(self):
        run = make_run(
            status="failed",
            http_status=500,
            errors=["HTTPStatusError: 500 Server Error"],
            message="HTTPStatusError",
        )
        self._day_logs([run])
        issues = api_issues_for_day(self.today)
        kinds = {issue["kind"] for issue in issues}
        self.assertEqual(kinds, {"http", "run"})
        run_issue = next(i for i in issues if i["kind"] == "run")
        self.assertEqual(run_issue["status"], "failed")
        self.assertIn("HTTPStatusError", run_issue["message"])

    def test_log_report_command_prints_issues(self):
        run = make_run(api_hits=2, found=3)
        run["pages_hit"] = [
            {"page": 0, "http_status": 200, "found": 3},
            {"page": 1, "http_status": 503, "found": 0},
        ]
        self._day_logs([run])
        out = io.StringIO()
        call_command("log_report", "--day", self.today.isoformat(), stdout=out)
        text = out.getvalue()
        self.assertIn("afriwork", text)
        self.assertIn("503", text)
        self.assertIn("page=1", text)

    def test_log_report_command_clean_day(self):
        self._day_logs([make_run()])
        out = io.StringIO()
        call_command("log_report", "--day", self.today.isoformat(), stdout=out)
        self.assertIn("All API responses returned 200", out.getvalue())

    def test_log_report_warns_when_website_logs_are_missing(self):
        # Master row exists but the detail-log hook wrote no website logs —
        # the report must not claim "all 200".
        ScrapeLog.objects.create(
            day=self.today,
            status=ScrapeStatus.SUCCESS,
            run_count=0,
            api_hits=0,
            items_found=0,
            items_inserted=0,
            items_updated=0,
            items_skipped=0,
        )
        out = io.StringIO()
        call_command("log_report", "--day", self.today.isoformat(), stdout=out)
        self.assertIn("No website logs recorded", out.getvalue())

    def test_log_report_warns_when_no_master_log(self):
        out = io.StringIO()
        other_day = (self.today - timedelta(days=1)).isoformat()
        call_command("log_report", "--day", other_day, stdout=out)
        self.assertIn("no master log", out.getvalue())


class DayLogTests(TestCase):
    """The master + per-website rollups stay correct and lean."""

    def setUp(self):
        self.source = Source.objects.create(
            slug="afriwork",
            name="Afriwork",
            endpoint="https://example.com/graphql",
        )
        self.today = timezone.localdate()

    def test_master_rollup_is_lean_and_references_site_log(self):
        scraper = GraphQLScraper(self.source)
        run = make_run(api_hits=2, found=3)
        site_log_id = scraper.record_detail_log(run, self.today)
        master = scraper._update_master_day_log(run, self.today, site_log_id)

        self.assertEqual(master.run_count, 1)
        self.assertEqual(master.api_hits, 2)
        bucket = master.website("afriwork")
        self.assertEqual(bucket["table"], "AfriworkScrapeLog")
        self.assertEqual(bucket["log_id"], site_log_id)
        self.assertEqual(bucket["status"], "success")
        self.assertNotIn("scraped_log", bucket)  # lean: detail lives in the site log
        self.assertFalse(hasattr(master, "scraped_log"))

        site_log = AfriworkScrapeLog.objects.get()
        self.assertEqual(len(site_log.scraped_log), 1)

    def test_master_and_site_accumulate_across_runs(self):
        scraper = GraphQLScraper(self.source)
        site_log_id = None
        for _ in range(2):
            run = make_run(api_hits=2, found=3)
            site_log_id = scraper.record_detail_log(run, self.today)
            master = scraper._update_master_day_log(run, self.today, site_log_id)
        master.refresh_from_db()
        self.assertEqual(master.run_count, 2)
        self.assertEqual(master.api_hits, 4)
        self.assertEqual(master.items_found, 6)
        site_log = AfriworkScrapeLog.objects.get()
        self.assertEqual(site_log.run_count, 2)
        self.assertEqual(len(site_log.scraped_log), 2)

    def test_bucket_status_falls_back_to_run_status_without_site_log(self):
        scraper = GraphQLScraper(self.source)
        run = make_run(status="failed", http_status=500, errors=["boom"])
        master = scraper._update_master_day_log(run, self.today, None)
        bucket = master.website("afriwork")
        self.assertEqual(bucket["status"], "failed")
        self.assertIsNone(bucket["table"])

    def test_incremental_stop_safe_reads_site_log_status(self):
        scraper = GraphQLScraper(self.source)
        self.assertFalse(scraper._incremental_stop_safe())  # no site log yet
        site_log = AfriworkScrapeLog.objects.create(
            source=self.source,
            day=self.today,
            status=ScrapeStatus.SUCCESS,
            run_count=1,
            api_hits=1,
            items_found=1,
            items_inserted=1,
        )
        self.assertTrue(scraper._incremental_stop_safe())
        site_log.status = ScrapeStatus.FAILED
        site_log.save()
        self.assertFalse(scraper._incremental_stop_safe())

    def test_last_run_delegates_to_site_log(self):
        scraper = GraphQLScraper(self.source)
        scraper.record_detail_log(make_run(found=3), self.today)
        self.assertEqual(scraper.last_run()["items_found"], 3)
        self.assertIsNone(scraper.last_run(self.today + timedelta(days=1)))
