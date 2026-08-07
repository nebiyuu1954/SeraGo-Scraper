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
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from core.models import (
    AfriworkScrapeLog,
    EthioJobsJob,
    EthioJobsScrapeLog,
    GeezJob,
    GeezScrapeLog,
    HaHuJob,
    HaHuScrapeLog,
    ReporterJob,
    ReporterScrapeLog,
    ScrapeLog,
    ScrapeStatus,
    ScrapedItem,
    Source,
)
from core.reporting import api_issues_for_day
from core.scrapers.base import ScrapeError, transform_job_type_code
from core.scrapers.geezjobs import GeezJobsScraper
from core.scrapers.graphql import GraphQLScraper
from core.scrapers.hahujobs import HaHuJobsScraper
from core.scrapers.html import HtmlScraper
from core.scrapers.reporterjobs import ReporterJobsScraper
from core.scrapers.rest import RestJsonScraper
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

# A faithful sample of ONE EthioJobs job object (a subset of the live
# capture — the flattened paths must stay inside ethiojobs.json).
ETHIOJOBS_SAMPLE = {
    "id": "eyJpdiI6IlJoUWFkaURaL0JqOUlvUlRIOVVpMFE9PSIsInZhbHVlIjoiUm1zQmpxRE5zTzllTlVYMmIvVFRtZz09IiwibWFjIjoiMGEzMTJlM2Y0YmMxNzQ3YzYzOGU5ZWFhYmU2YjMwZDdjY2Y4ZmJlNDVmNzBiMjgxOTUwNTcwOWI5ZjkyZjEwNiIsInRhZyI6IiJ9",
    "title": "Senior International Banking Officer",
    "slug": "asJrYsRdI8-senior-international-banking-officer",
    "type": 8,
    "date_published": "2026-08-07T10:52:43.000000Z",
    "level": "3",
    "location_type": "Office",
    "description": "<p>Some job description</p>",
    "state": "Addis Ababa",
    "catalogs": [{"id": "abc123", "name": "Banking and Insurance", "options": 346}],
    "date_expiry": "2026-08-13T23:59:59.000000Z",
    "company": {
        "id": "company-encrypted-id",
        "listings_id": "459d3a24c3",
        "slug": "ahadu-bank-sc",
        "parent_id": 0,
        "type": "regular",
        "name": "Ahadu Bank S.C",
        "name_legal": "Ahadu Bank S.C",
        "logo": "company-logo/809736/images__1_.png",
        "phone": None,
        "email": "mikre.a@ahadubank.com",
        "website": "",
        "description": "<p>About the company</p>",
        "industry_id": "industry-encrypted-id",
        "contacts": "{\"phone\":null,\"email\":\"mikre.a@ahadubank.com\"}",
        "social": "[]",
        "segments": None,
        "activated_at": "2022-07-31 08:53:01",
        "verified_at": None,
        "created_by": None,
        "updated_by": "updated-by-encrypted",
        "deleted_by": None,
        "deleted_at": None,
        "created_at": "2024-07-19 04:02:07",
        "updated_at": "2025-08-07 08:32:42",
        "banner_image": "company-banner/809736/ahadu-card-bank-1.jpg",
        "slogan": None,
        "date_founded": None,
        "total_employees": None,
        "local_employees": None,
        "videos": None,
        "benefits": None,
        "quote_details": None,
        "quote_image": None,
        "featured": "1",
        "show_tour": 1,
    },
    "application_form": None,
    "career_page_link": None,
    "application_method": "ATS",
    "application_email": None,
}

ETHIOJOBS_SAMPLE_PATHS = [
    "application_email",
    "application_form",
    "application_method",
    "career_page_link",
    "catalogs.id",
    "catalogs.name",
    "catalogs.options",
    "company.activated_at",
    "company.banner_image",
    "company.benefits",
    "company.contacts",
    "company.created_at",
    "company.created_by",
    "company.date_founded",
    "company.deleted_at",
    "company.deleted_by",
    "company.description",
    "company.email",
    "company.featured",
    "company.id",
    "company.industry_id",
    "company.listings_id",
    "company.local_employees",
    "company.logo",
    "company.name",
    "company.name_legal",
    "company.parent_id",
    "company.phone",
    "company.quote_details",
    "company.quote_image",
    "company.segments",
    "company.show_tour",
    "company.slogan",
    "company.slug",
    "company.social",
    "company.total_employees",
    "company.type",
    "company.updated_at",
    "company.updated_by",
    "company.verified_at",
    "company.videos",
    "company.website",
    "date_expiry",
    "date_published",
    "description",
    "id",
    "level",
    "location_type",
    "slug",
    "state",
    "title",
    "type",
]

# A faithful sample of ONE HaHuJobs job object (a subset of the live capture —
# the flattened paths must stay inside hahujobs.json). HaHuJobs is an
# aggregator: ``source`` names the upstream website the listing came from
# (hahujobs_telegram here; ethiojobs-sourced listings are skipped by the
# scraper because EthioJobs is scraped directly).
HAHUJOBS_SAMPLE = {
    "id": "6a60ab445b67c401472a0734",
    "title": "Social Media Sales Representative",
    "total_web_view_count": 127,
    "telegram_view_count": 9070,
    "total_view_count": 9197,
    "type": "full_time",
    "max_years_of_experience": None,
    "years_of_experience": 2,
    "summary": "TVET Level IV or Bachelor's Degree in Marketing with relevant work experience",
    "salary": None,
    "deadline": "2026-08-08T00:00:00+00:00",
    "expired": False,
    "location": None,
    "source": "hahujobs_telegram",
    "application_method": "link",
    "application_url": "https://t.me/Dayotgb",
    "application_email": "",
    "number_of_applicants": 1,
    "approved_on": "2026-07-22T12:50:48.27093+00:00",
    "job_cities": [
        {"city": {"name": "Addis Ababa", "region": {"name": "Addis Ababa", "id": "X1dBPES-3TX_YrKPG51jX"}}}
    ],
    "entity": {"logo": None, "name": "Dayot General Business Plc", "id": "aL6T2Ksal9l1Cz1"},
    "sub_sector": {
        "name": "Business Sales and Marketing",
        "sector": {
            "name": "Business",
            "id": "QuF5r_hhUgdYBqID2vfLM",
            "icon_class": "business-time",
            "icon_code": "f64a",
        },
    },
    "area": None,
    "isco_08": {
        "isco_08_code": "3322",
        "title_en": "Commercial Sales Representatives",
        "title_am": "የገበያ ሽያጭ ወኪሎች",
    },
    "soc_2010": {
        "title": "Sales Representatives, Wholesale and Manufacturing, Except Technical and Scientific Products",
        "onetsoc_code": "41-4012.00",
    },
    "esco_code": "3322.1",
}

HAHUJOBS_SAMPLE_PATHS = [
    "application_email",
    "application_method",
    "application_url",
    "approved_on",
    "area",
    "deadline",
    "entity.id",
    "entity.logo",
    "entity.name",
    "esco_code",
    "expired",
    "id",
    "isco_08.isco_08_code",
    "isco_08.title_am",
    "isco_08.title_en",
    "job_cities.city.name",
    "job_cities.city.region.id",
    "job_cities.city.region.name",
    "location",
    "max_years_of_experience",
    "number_of_applicants",
    "salary",
    "soc_2010.onetsoc_code",
    "soc_2010.title",
    "source",
    "sub_sector.name",
    "sub_sector.sector.icon_class",
    "sub_sector.sector.icon_code",
    "sub_sector.sector.id",
    "sub_sector.sector.name",
    "summary",
    "telegram_view_count",
    "title",
    "total_view_count",
    "total_web_view_count",
    "type",
    "years_of_experience",
]

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

# A faithful minimal listing page for GeezJobs (server-side HTML). It mirrors
# the live /search-jobs markup: one .opportunity-card per listing, info rows
# as `i[data-lucide=...]` + sibling span/a, the site's honeypot (.trap-field),
# and the search-filter UI. Two cards: one posted today, one 3 days ago (the
# client-side today filter must drop the old one and end the sweep on it).
GEEZJOBS_SAMPLE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Search jobs in Ethiopia</title></head>
<body>
  <div class="trap-field" aria-hidden="true">
    <a href="/security/report-scraping" rel="nofollow">Bot Interface</a>
    <input type="checkbox" name="is_bot" tabindex="-1" autocomplete="off">
  </div>
  <section class="bg-white pt-24 pb-6">
    <div class="filter-group" data-filter-key="std">
      <button type="button" class="filter-btn"><span class="label">Field of Study</span></button>
    </div>
    <form action="/search-jobs" method="GET" class="flex-grow"></form>
  </section>
  <section class="py-12">
    <div class="opportunity-card group bg-white p-6 md:p-8 rounded-[2rem]">
      <div class="grid grid-cols-[auto_1fr] gap-x-4">
        <div class="w-16 h-16 rounded-2xl overflow-hidden">
          <img src="/image/logo/4608638Dololo_Import.jpg" alt="Dololo Import And Export PLC Logo" class="w-full h-full object-contain">
        </div>
        <div class="pt-1">
          <h3 class="text-lg md:text-xl font-black">
            <a href="/job-detail/senior-finance-officer-dololo-import-and-export-plc">Senior Finance Officer</a>
          </h3>
          <div class="flex flex-wrap items-center gap-x-6 gap-y-2">
            <div class="flex items-center gap-2">
              <i data-lucide="building-2" class="w-3.5 h-3.5 text-slate-400"></i>
              <a href="/company/dololo-import-and-export-plc" class="hover:text-brand-primary">Dololo Import And Export PLC</a>
            </div>
            <div class="flex items-center gap-2">
              <i data-lucide="map-pin" class="w-3.5 h-3.5 text-slate-400"></i>
              <span>Addis Ababa                                                            - Ethiopia                                                        </span>
            </div>
            <div class="flex items-center gap-2 text-rose-500/80">
              <i data-lucide="calendar-x" class="w-3.5 h-3.5"></i>
              <span>Deadline: September 7, 2026</span>
            </div>
          </div>
        </div>
        <div class="col-span-2 mt-4 flex flex-col md:flex-row justify-between gap-6 border-t pt-4">
          <div class="flex flex-wrap gap-2">
            <div class="flex items-center gap-2 px-3 py-1.5 bg-slate-50 rounded-xl">
              <i data-lucide="briefcase" class="w-3.5 h-3.5 text-slate-400"></i>
              <span class="text-[10px] md:text-xs font-bold">Full-time / Permanent</span>
            </div>
            <div class="flex items-center gap-2 px-3 py-1.5 bg-slate-50 rounded-xl">
              <i data-lucide="award" class="w-3.5 h-3.5 text-slate-400"></i>
              <span class="text-[10px] md:text-xs font-bold">4+ Years</span>
            </div>
            <div class="flex items-center gap-2 px-3 py-1.5 bg-slate-50 rounded-xl">
              <i data-lucide="calendar-plus" class="w-3.5 h-3.5 text-emerald-500/60"></i>
              <span class="text-[10px] md:text-xs font-bold">Posted: 3 min ago</span>
            </div>
          </div>
        </div>
      </div>
    </div>
    <div class="opportunity-card group bg-white p-6 md:p-8 rounded-[2rem]">
      <div class="grid grid-cols-[auto_1fr] gap-x-4">
        <div class="w-16 h-16 rounded-2xl overflow-hidden">
          <span class="text-2xl md:text-4xl font-black text-slate-400 uppercase select-none">4</span>
        </div>
        <div class="pt-1">
          <h3 class="text-lg md:text-xl font-black">
            <a href="/job-detail/office-engineer-4b-trading-plc">Office Engineer</a>
          </h3>
          <div class="flex flex-wrap items-center gap-x-6 gap-y-2">
            <div class="flex items-center gap-2">
              <i data-lucide="building-2" class="w-3.5 h-3.5 text-slate-400"></i>
              <a href="/company/4b-trading-plc" class="hover:text-brand-primary">4B Trading PLC</a>
            </div>
            <div class="flex items-center gap-2">
              <i data-lucide="map-pin" class="w-3.5 h-3.5 text-slate-400"></i>
              <span>Sendafa - Ethiopia</span>
            </div>
            <div class="flex items-center gap-2 text-rose-500/80">
              <i data-lucide="calendar-x" class="w-3.5 h-3.5"></i>
              <span>Deadline: August 16, 2026</span>
            </div>
          </div>
        </div>
        <div class="col-span-2 mt-4 flex flex-col md:flex-row justify-between gap-6 border-t pt-4">
          <div class="flex flex-wrap gap-2">
            <div class="flex items-center gap-2 px-3 py-1.5 bg-slate-50 rounded-xl">
              <i data-lucide="briefcase" class="w-3.5 h-3.5 text-slate-400"></i>
              <span class="text-[10px] md:text-xs font-bold">Full-time / Permanent</span>
            </div>
            <div class="flex items-center gap-2 px-3 py-1.5 bg-slate-50 rounded-xl">
              <i data-lucide="award" class="w-3.5 h-3.5 text-slate-400"></i>
              <span class="text-[10px] md:text-xs font-bold">3/5+ Years</span>
            </div>
            <div class="flex items-center gap-2 px-3 py-1.5 bg-slate-50 rounded-xl">
              <i data-lucide="calendar-plus" class="w-3.5 h-3.5 text-emerald-500/60"></i>
              <span class="text-[10px] md:text-xs font-bold">Posted: 3 days ago</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</body></html>
"""

# The raw card dict GeezJobsScraper.parse() produces for the FIRST card above.
# published_at is estimated from the 'Posted: X ago' chip, so tests assert on
# the stable fields and check the timestamp separately.
GEEZJOBS_SAMPLE = {
    "title": "Senior Finance Officer",
    "slug": "senior-finance-officer-dololo-import-and-export-plc",
    "url": "https://geezjobs.com/job-detail/senior-finance-officer-dololo-import-and-export-plc",
    "company": "Dololo Import And Export PLC",
    "location": "Addis Ababa",
    "country": "Ethiopia",
    "logo": "https://geezjobs.com/image/logo/4608638Dololo_Import.jpg",
    "employment_text": "Full-time / Permanent",
    "job_time": "full_time",
    "job_type": "permanent",
    "experience_text": "4+ Years",
    "min_experience_years": 4,
    "max_experience_years": None,
    "posted_text": "Posted: 3 min ago",
    "published_at": "2026-08-07T11:00:00+03:00",
    "deadline_text": "Deadline: September 7, 2026",
    "deadline": "2026-09-07T00:00:00+03:00",
}

GEEZJOBS_SAMPLE_PATHS = [
    "company",
    "country",
    "deadline",
    "deadline_text",
    "employment_text",
    "experience_text",
    "job_time",
    "job_type",
    "location",
    "logo",
    "max_experience_years",
    "min_experience_years",
    "posted_text",
    "published_at",
    "slug",
    "title",
    "url",
]

# A faithful minimal listing page for Ethiopian Reporter Jobs (WordPress / Noo
# Job Board theme). It mirrors the live /jobs-in-ethiopia/ markup: one
# article.noo_job per listing with h3.loop-item-title, .job-company, .job-type,
# .job-location and a <time class="entry-date" datetime="..."> carrying the
# exact posted timestamp plus the posted/closing date spans. The listings
# archive container (div.jobs.posts-loop) marks a real listings page. Two
# cards: one posted today, one 3 days ago (the client-side today filter must
# drop the old one and end the sweep on it).
REPORTER_SAMPLE_HTML = """<!DOCTYPE html>
<html lang="en-US"><head><meta charset="UTF-8"><title>Job Vacancy In Ethiopia - Ethiopian Reporter Jobs</title></head>
<body>
  <div class="jobs posts-loop">
    <div class="posts-loop-content noo-job-list-row">
      <article class="nextajax-item noo_job style-1 post-284574 type-noo_job status-publish hentry" data-url="https://www.ethiopianreporterjobs.com/jobs-in-ethiopia/284574/">
        <h3 class="loop-item-title"><a href="https://www.ethiopianreporterjobs.com/jobs-in-ethiopia/284574/">Property Administrator</a></h3>
        <div class="loop-item-wrap list">
          <span class="job-company"><a href="#"><span>LANCET GENERAL HOSPITAL</span></a></span>
          <span class="job-type"><a href="/job-type/full-time/"><i class="fa fa-bookmark"></i><span>Full Time</span></a></span>
          <span class="job-location" itemprop="jobLocation"><a href="/job-location/jobs-in-addis-ababa/"><em>Addis Ababa</em></a></span>
          <span class="job-date"><time class="entry-date" datetime="2026-08-07T06:00:53+03:00"><span class="job-date__posted">August 7, 2026</span><span class="job-date__closing"> - August 14, 2026</span></time></span>
        </div>
      </article>
      <article class="nextajax-item noo_job style-1 post-285000 type-noo_job status-publish hentry" data-url="https://www.ethiopianreporterjobs.com/jobs-in-ethiopia/285000/">
        <h3 class="loop-item-title"><a href="https://www.ethiopianreporterjobs.com/jobs-in-ethiopia/285000/">Office Engineer</a></h3>
        <div class="loop-item-wrap list">
          <span class="job-company"><a href="#"><span>4B Trading PLC</span></a></span>
          <span class="job-type"><a href="/job-type/contract/"><i class="fa fa-bookmark"></i><span>Contract</span></a></span>
          <span class="job-location" itemprop="jobLocation"><a href="/job-location/bahir-dar/"><em>Bahir Dar</em></a></span>
          <span class="job-date"><time class="entry-date" datetime="2026-08-04T06:00:53+03:00"><span class="job-date__posted">August 4, 2026</span><span class="job-date__closing"> - August 12, 2026</span></time></span>
        </div>
      </article>
    </div>
  </div>
</body></html>
"""

# The raw card dict ReporterJobsScraper.parse() produces for the FIRST card
# above. published_at comes from the exact <time datetime> attribute.
REPORTER_SAMPLE = {
    "post_id": "284574",
    "title": "Property Administrator",
    "url": "https://www.ethiopianreporterjobs.com/jobs-in-ethiopia/284574/",
    "company": "LANCET GENERAL HOSPITAL",
    "job_type_text": "Full Time",
    "job_type": "full_time",
    "location": "Addis Ababa",
    "posted_text": "August 7, 2026",
    "published_at": "2026-08-07T06:00:53+03:00",
    "deadline_text": "August 14, 2026",
    "deadline": "2026-08-14T00:00:00+03:00",
}

REPORTER_SAMPLE_PATHS = [
    "company",
    "deadline",
    "deadline_text",
    "job_type",
    "job_type_text",
    "location",
    "post_id",
    "posted_text",
    "published_at",
    "title",
    "url",
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


    def test_ethiojobs_snapshot_exists_and_contains_core_structure(self):
        snapshot = load_structure("ethiojobs")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["source"], "ethiojobs")
        missing = set(ETHIOJOBS_SAMPLE_PATHS) - set(snapshot["fields"])
        self.assertFalse(missing, f"EthioJobs snapshot is missing core fields: {sorted(missing)}")

    def test_extract_structure_matches_ethiojobs_sample(self):
        self.assertEqual(extract_structure(ETHIOJOBS_SAMPLE), ETHIOJOBS_SAMPLE_PATHS)

    def test_job_type_code_transform(self):
        self.assertEqual(transform_job_type_code(8), "OTHER")
        self.assertEqual(transform_job_type_code(1), "FULL_TIME")
        self.assertEqual(transform_job_type_code(3), "CONTRACT")
        self.assertIsNone(transform_job_type_code(None))


class RestScraperTests(TestCase):
    """The REST scraper: config-driven today filter, boundary, generic day log."""

    def setUp(self):
        self.source = Source.objects.create(
            slug="ethiojobs",
            name="EthioJobs",
            endpoint="https://api.ethiojobs.net/ethiojobs/api/job-board/jobs",
            scraper_type="rest",
            only_today=True,
            field_mapping={
                "external_id": "slug",
                "title": "title",
                "location": {"path": "state", "transforms": ["clean_text"]},
                "job_type": {"path": "type", "transforms": ["job_type_code"]},
                "published_at": {"path": "date_published", "transforms": ["parse_datetime"]},
                "deadline": {"path": "date_expiry", "transforms": ["parse_datetime"]},
            },
            pagination={
                "page_size": 10,
                "results_path": "data",
                "page_1_based": True,
                "date_filter": {"field": "published_at"},
                "max_pages": 100,
            },
        )
        self.scraper = RestJsonScraper(self.source)

    @staticmethod
    def _item(days_ago=0):
        """A normalized item published ``days_ago`` days from now (UTC)."""
        from datetime import timedelta

        return {
            "external_id": "slug-%d" % days_ago,
            "title": "Job %d" % days_ago,
            "location": "Addis Ababa",
            "job_type": "OTHER",
            "published_at": timezone.now() - timedelta(days=days_ago),
            "raw_data": {"slug": "slug-%d" % days_ago},
        }

    def test_query_params_are_1_based(self):
        params = self.scraper._query_params(0)
        self.assertEqual(params["page"], 1)
        self.assertEqual(params["limit"], 10)
        self.assertEqual(self.scraper._query_params(6)["page"], 7)

    def test_keep_item_drops_pre_today(self):
        self.assertTrue(self.scraper._keep_item(self._item(0)))
        self.assertFalse(self.scraper._keep_item(self._item(1)))
        self.assertFalse(self.scraper._keep_item(self._item(2)))

    def test_past_today_boundary(self):
        # A page with only pre-today items marks the boundary.
        self.assertTrue(
            self.scraper._past_today_boundary(6, [self._item(1), self._item(2)])
        )
        # A page with today's items (even mixed) does not.
        self.assertFalse(
            self.scraper._past_today_boundary(0, [self._item(0), self._item(1)])
        )
        self.assertFalse(self.scraper._past_today_boundary(0, [self._item(0)]))

    def test_save_detail_persists_everything_and_links_master(self):
        item = self.scraper.normalize(ETHIOJOBS_SAMPLE)
        item["raw_data"] = ETHIOJOBS_SAMPLE
        master_item = ScrapedItem.objects.create(
            source=self.source,
            external_id=item["external_id"],
            title=item["title"],
            job_number=1,
            numbered_on=timezone.localdate(),
        )
        self.scraper._save_detail(item, master_item)
        detail = EthioJobsJob.objects.get(external_id=item["external_id"])
        self.assertEqual(detail.api_id, ETHIOJOBS_SAMPLE["id"])
        self.assertEqual(detail.slug, ETHIOJOBS_SAMPLE["slug"])
        self.assertEqual(detail.company["name"], "Ahadu Bank S.C")
        self.assertEqual(detail.state, "Addis Ababa")
        self.assertEqual(detail.job_number, 1)
        master_item.refresh_from_db()
        self.assertEqual(master_item.ethiojobs_job_id, detail.pk)

    def test_generic_record_detail_log_writes_ethiojobs_site_log(self):
        scraper = RestJsonScraper(self.source)
        run = make_run(api_hits=2, found=3)
        log_id = scraper.record_detail_log(run, timezone.localdate())
        self.assertIsNotNone(log_id)
        site_log = EthioJobsScrapeLog.objects.get()
        self.assertEqual(len(site_log.scraped_log), 1)
        self.assertEqual(site_log.api_hits, 2)
        # The master bucket references the right table.
        master = scraper._update_master_day_log(run, timezone.localdate(), log_id)
        bucket = master.website("ethiojobs")
        self.assertEqual(bucket["table"], "EthioJobsScrapeLog")
        self.assertEqual(bucket["log_id"], log_id)
        self.assertEqual(bucket["status"], "success")


class HaHuJobsScraperTests(TestCase):
    """The HaHuJobs aggregator scraper: structure, detail row, ethiojobs skip, site log."""

    def setUp(self):
        self.source = Source.objects.create(
            slug="hahujobs",
            name="HaHu Jobs",
            endpoint="https://graph.aggregator.hahu.jobs/v1/graphql",
            scraper_type="graphql",
            only_today=True,
            field_mapping={
                "external_id": "id",
                "title": "title",
                "description": {"path": "summary", "transforms": ["strip_html"]},
                "company": "entity.name",
                "salary": "salary",
                "job_type": {"path": "type", "transforms": ["upper"]},
                "published_at": {"path": "approved_on", "transforms": ["parse_datetime"]},
                "deadline": {"path": "deadline", "transforms": ["parse_datetime"]},
            },
            pagination={
                "page_size": 20,
                "results_path": "data.jobs",
                "date_filter": {"field": "approved_on", "from_var": "from", "to_var": "to"},
                "max_pages": 50,
            },
        )
        self.scraper = HaHuJobsScraper(self.source)

    def test_extract_structure_matches_hahujobs_sample(self):
        self.assertEqual(extract_structure(HAHUJOBS_SAMPLE), HAHUJOBS_SAMPLE_PATHS)

    def test_hahujobs_snapshot_exists_and_contains_core_structure(self):
        snapshot = load_structure("hahujobs")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["source"], "hahujobs")
        missing = set(HAHUJOBS_SAMPLE_PATHS) - set(snapshot["fields"])
        self.assertFalse(missing, f"HaHuJobs snapshot is missing core fields: {sorted(missing)}")

    def test_normalize_derives_location_and_coerces_salary(self):
        item = self.scraper.normalize(HAHUJOBS_SAMPLE)
        # Location comes from the nested job_cities list (dotted paths can't
        # reach it), and the lowercase type is uppercased to the shared enum.
        self.assertEqual(item["location"], "Addis Ababa")
        self.assertEqual(item["job_type"], "FULL_TIME")
        self.assertEqual(item["url"], "https://www.hahu.jobs/jobs/6a60ab445b67c401472a0734")
        self.assertIsNone(item.get("salary"))  # None salary stays None
        with_salary = dict(HAHUJOBS_SAMPLE)
        with_salary["salary"] = 80000
        self.assertEqual(self.scraper.normalize(with_salary)["salary"], "80000")

    def test_save_detail_persists_everything_and_links_master(self):
        item = self.scraper.normalize(HAHUJOBS_SAMPLE)
        item["raw_data"] = HAHUJOBS_SAMPLE
        master_item = ScrapedItem.objects.create(
            source=self.source,
            external_id=item["external_id"],
            title=item["title"],
            job_number=1,
            numbered_on=timezone.localdate(),
        )
        self.scraper._save_detail(item, master_item)
        detail = HaHuJob.objects.get(external_id=item["external_id"])
        self.assertEqual(detail.entity_name, "Dayot General Business Plc")
        self.assertEqual(detail.type, "FULL_TIME")
        self.assertEqual(detail.cities, ["Addis Ababa"])
        self.assertEqual(detail.source, "hahujobs_telegram")
        self.assertEqual(detail.application_url, "https://t.me/Dayotgb")
        self.assertEqual(detail.job_number, 1)
        master_item.refresh_from_db()
        self.assertEqual(master_item.hahujobs_job_id, detail.pk)

    def test_save_items_skips_ethiojobs_sourced(self):
        ethiojobs_item = self.scraper.normalize(HAHUJOBS_SAMPLE)
        ethiojobs_item["external_id"] = "ethiojobs-1"
        ethiojobs_item["raw_data"] = {**HAHUJOBS_SAMPLE, "source": "ethiojobs"}

        telegram_item = self.scraper.normalize(HAHUJOBS_SAMPLE)
        telegram_item["external_id"] = "telegram-1"
        telegram_item["raw_data"] = HAHUJOBS_SAMPLE

        inserted, updated, skipped, errors = self.scraper.save_items(
            [ethiojobs_item, telegram_item]
        )
        self.assertEqual(inserted, 1)
        self.assertEqual(updated, 0)
        self.assertEqual(skipped, 1)
        self.assertEqual(errors, [])
        self.assertFalse(ScrapedItem.objects.filter(external_id="ethiojobs-1").exists())
        self.assertTrue(ScrapedItem.objects.filter(external_id="telegram-1").exists())
        self.assertEqual(HaHuJob.objects.count(), 1)

    def test_generic_record_detail_log_writes_hahujobs_site_log(self):
        run = make_run(api_hits=2, found=3)
        log_id = self.scraper.record_detail_log(run, timezone.localdate())
        self.assertIsNotNone(log_id)
        site_log = HaHuScrapeLog.objects.get()
        self.assertEqual(len(site_log.scraped_log), 1)
        self.assertEqual(site_log.api_hits, 2)
        # The master bucket references the right table.
        master = self.scraper._update_master_day_log(run, timezone.localdate(), log_id)
        bucket = master.website("hahujobs")
        self.assertEqual(bucket["table"], "HaHuScrapeLog")
        self.assertEqual(bucket["log_id"], log_id)
        self.assertEqual(bucket["status"], "success")

    def test_sweep_continues_past_all_ethiojobs_page(self):
        # The reason the ethiojobs filter lives in save_items (not
        # _keep_item): a page made entirely of ethiojobs listings must NOT
        # look like an empty page and truncate the sweep. Page 0 is all
        # ethiojobs; the sweep must still reach page 1's real listing.
        ethiojobs_item = self.scraper.normalize(HAHUJOBS_SAMPLE)
        ethiojobs_item["external_id"] = "ethiojobs-1"
        ethiojobs_item["raw_data"] = {**HAHUJOBS_SAMPLE, "source": "ethiojobs"}

        telegram_item = self.scraper.normalize(HAHUJOBS_SAMPLE)
        telegram_item["external_id"] = "telegram-1"
        telegram_item["raw_data"] = HAHUJOBS_SAMPLE

        pages = [
            ([ethiojobs_item], 200),  # page 0: all ethiojobs — keep sweeping
            ([telegram_item], 200),   # page 1: the real listing
            ([], 200),                # page 2: genuinely empty — stop here
        ]
        with mock.patch.object(self.scraper, "_page_items", side_effect=pages):
            self.scraper.scrape_many()

        self.assertTrue(ScrapedItem.objects.filter(external_id="telegram-1").exists())
        self.assertFalse(ScrapedItem.objects.filter(external_id="ethiojobs-1").exists())
        self.assertEqual(HaHuJob.objects.count(), 1)


class GeezJobsScraperTests(TestCase):
    """The GeezJobs HTML scraper: card parsing, today filter, detail row, site log."""

    def setUp(self):
        self.source = Source.objects.create(
            slug="geezjobs",
            name="GeezJobs",
            base_url="https://geezjobs.com",
            endpoint="https://geezjobs.com/search-jobs",
            scraper_type="html",
            only_today=True,
            field_mapping={
                "external_id": "slug",
                "title": "title",
                "company": "company",
                "location": "location",
                "job_type": {"path": "job_time", "transforms": ["upper"]},
                "url": "url",
                "published_at": {"path": "published_at", "transforms": ["parse_datetime"]},
                "deadline": {"path": "deadline", "transforms": ["parse_datetime"]},
            },
            pagination={
                "page_size": 15,
                "page_1_based": True,
                "page_key": "page",
                "date_filter": {"field": "published_at"},
                "max_pages": 20,
            },
        )
        self.scraper = GeezJobsScraper(self.source)

    def test_extract_structure_matches_geezjobs_sample(self):
        self.assertEqual(extract_structure(GEEZJOBS_SAMPLE), GEEZJOBS_SAMPLE_PATHS)

    def test_geezjobs_snapshot_exists_and_contains_core_structure(self):
        snapshot = load_structure("geezjobs")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["source"], "geezjobs")
        missing = set(GEEZJOBS_SAMPLE_PATHS) - set(snapshot["fields"])
        self.assertFalse(missing, f"GeezJobs snapshot is missing core fields: {sorted(missing)}")

    def test_page_url_omits_param_on_page_1(self):
        # Page 1 is the bare /search-jobs URL; ?page=N starts at page 2.
        self.assertEqual(self.scraper._page_url(0), "https://geezjobs.com/search-jobs")
        self.assertEqual(self.scraper._page_url(1), "https://geezjobs.com/search-jobs?page=2")
        self.assertEqual(self.scraper._page_url(6), "https://geezjobs.com/search-jobs?page=7")

    def test_parse_extracts_cards_from_html(self):
        from bs4 import BeautifulSoup

        raw = BeautifulSoup(GEEZJOBS_SAMPLE_HTML, "html.parser")
        items = self.scraper.parse(raw)
        self.assertEqual(len(items), 2)

        first = items[0]
        self.assertEqual(first["title"], "Senior Finance Officer")
        self.assertEqual(first["slug"], "senior-finance-officer-dololo-import-and-export-plc")
        self.assertEqual(
            first["url"],
            "https://geezjobs.com/job-detail/senior-finance-officer-dololo-import-and-export-plc",
        )
        self.assertEqual(first["company"], "Dololo Import And Export PLC")
        self.assertEqual(first["location"], "Addis Ababa")
        self.assertEqual(first["country"], "Ethiopia")
        self.assertEqual(
            first["logo"], "https://geezjobs.com/image/logo/4608638Dololo_Import.jpg"
        )
        self.assertEqual(first["employment_text"], "Full-time / Permanent")
        self.assertEqual(first["job_time"], "full_time")
        self.assertEqual(first["job_type"], "permanent")
        self.assertEqual(first["experience_text"], "4+ Years")
        self.assertEqual(first["min_experience_years"], 4)
        self.assertIsNone(first["max_experience_years"])
        self.assertEqual(first["deadline_text"], "Deadline: September 7, 2026")
        # Parsed deadline = local midnight; build the expected offset from the
        # active timezone so the assertion survives a TIME_ZONE change.
        expected_deadline = timezone.make_aware(datetime(2026, 9, 7)).isoformat()
        self.assertEqual(first["deadline"], expected_deadline)
        # Estimated from the posted-ago chip: roughly now.
        self.assertIsNotNone(first["published_at"])

        # The letter-placeholder card (no logo img) still parses.
        second = items[1]
        self.assertEqual(second["title"], "Office Engineer")
        self.assertEqual(second["location"], "Sendafa")
        self.assertEqual(second["logo"], "")
        self.assertEqual(second["experience_text"], "3/5+ Years")
        self.assertEqual(second["min_experience_years"], 3)
        self.assertEqual(second["max_experience_years"], 5)

    def test_chip_parsers(self):
        from core.scrapers.geezjobs import (
            _parse_deadline,
            _parse_employment,
            _parse_experience,
            _parse_posted,
        )

        # Experience: 'N+ Years', 'N/M Years', 'N Years', and 'N/M+ Years'.
        self.assertEqual(_parse_experience("3+ Years"), (3, None))
        self.assertEqual(_parse_experience("2/3 Years"), (2, 3))
        self.assertEqual(_parse_experience("6 Years"), (6, 6))
        self.assertEqual(_parse_experience("3/5+ Years"), (3, 5))
        self.assertEqual(_parse_experience("10+ Years"), (10, None))
        self.assertEqual(_parse_experience(""), (None, None))

        # Employment: time + type split, hyphens normalized to underscores.
        self.assertEqual(_parse_employment("Full-time / Permanent"), ("full_time", "permanent"))
        self.assertEqual(_parse_employment("Part-Time / Contract"), ("part_time", "contract"))
        self.assertEqual(_parse_employment("Full-time / Internship"), ("full_time", "internship"))
        self.assertEqual(_parse_employment(""), ("", ""))

        # Relative posted-ago chips estimate published_at; unknown text -> None.
        self.assertIsNotNone(_parse_posted("Posted: 3 min ago"))
        self.assertIsNotNone(_parse_posted("Posted: 1 hours ago"))
        self.assertIsNotNone(_parse_posted("Posted: 2 days ago"))
        self.assertIsNone(_parse_posted(""))
        self.assertIsNone(_parse_posted("Posted: whenever"))

        # Absolute deadlines parse to aware local midnight (full + abbreviated
        # month names).
        self.assertIsNotNone(_parse_deadline("Deadline: September 7, 2026"))
        self.assertIsNotNone(_parse_deadline("Deadline: Aug 7, 2026"))
        self.assertIsNone(_parse_deadline("nope"))

    def test_parse_raises_on_bot_check_page(self):
        from bs4 import BeautifulSoup

        # Honeypot present, NO cards and NO search UI -> likely bot detection.
        blocked = BeautifulSoup(
            '<html><body><div class="trap-field"><input type="checkbox" '
            'name="is_bot"></div></body></html>',
            "html.parser",
        )
        with self.assertRaises(ScrapeError) as ctx:
            self.scraper.parse(blocked)
        self.assertIn("bot detection", str(ctx.exception))

    def test_parse_returns_empty_on_genuine_empty_page(self):
        from bs4 import BeautifulSoup

        # Real listings page (filter UI present) but zero cards: legit empty.
        empty = BeautifulSoup(
            '<html><body><div class="filter-group"></div>'
            '<form action="/search-jobs"></form></body></html>',
            "html.parser",
        )
        self.assertEqual(self.scraper.parse(empty), [])

    def test_normalize_maps_card_fields(self):
        item = self.scraper.normalize(GEEZJOBS_SAMPLE)
        self.assertEqual(item["external_id"], "senior-finance-officer-dololo-import-and-export-plc")
        self.assertEqual(item["title"], "Senior Finance Officer")
        self.assertEqual(item["company"], "Dololo Import And Export PLC")
        self.assertEqual(item["location"], "Addis Ababa")
        # The time part of the employment chip maps to the shared enum.
        self.assertEqual(item["job_type"], "FULL_TIME")
        self.assertIsNotNone(item["published_at"])
        self.assertIsNotNone(item["deadline"])

    @staticmethod
    def _item(days_ago=0):
        """A normalized GeezJobs item published ``days_ago`` days from now."""
        return {
            "external_id": "slug-%d" % days_ago,
            "title": "Job %d" % days_ago,
            "location": "Addis Ababa",
            "job_type": "FULL_TIME",
            "published_at": timezone.now() - timedelta(days=days_ago),
            "raw_data": {"slug": "slug-%d" % days_ago},
        }

    def test_keep_item_drops_pre_today(self):
        self.assertTrue(self.scraper._keep_item(self._item(0)))
        self.assertFalse(self.scraper._keep_item(self._item(1)))
        self.assertFalse(self.scraper._keep_item(self._item(3)))

    def test_past_today_boundary(self):
        # A page whose kept items are all pre-today marks the boundary.
        self.assertTrue(
            self.scraper._past_today_boundary(2, [self._item(1), self._item(3)])
        )
        # A page with today's items (even mixed) does not.
        self.assertFalse(
            self.scraper._past_today_boundary(0, [self._item(0), self._item(1)])
        )

    def test_save_detail_persists_everything_and_links_master(self):
        item = self.scraper.normalize(GEEZJOBS_SAMPLE)
        item["raw_data"] = GEEZJOBS_SAMPLE
        master_item = ScrapedItem.objects.create(
            source=self.source,
            external_id=item["external_id"],
            title=item["title"],
            job_number=1,
            numbered_on=timezone.localdate(),
        )
        self.scraper._save_detail(item, master_item)
        detail = GeezJob.objects.get(external_id=item["external_id"])
        self.assertEqual(detail.company, "Dololo Import And Export PLC")
        self.assertEqual(detail.location, "Addis Ababa")
        self.assertEqual(detail.country, "Ethiopia")
        self.assertEqual(detail.job_time, "full_time")
        self.assertEqual(detail.job_type, "permanent")
        self.assertEqual(detail.employment_display, "full_time / permanent")
        self.assertEqual(detail.min_experience_years, 4)
        self.assertEqual(detail.deadline_text, "Deadline: September 7, 2026")
        self.assertEqual(detail.job_number, 1)
        master_item.refresh_from_db()
        self.assertEqual(master_item.geezjobs_job_id, detail.pk)

    def test_generic_record_detail_log_writes_geezjobs_site_log(self):
        run = make_run(api_hits=2, found=3)
        log_id = self.scraper.record_detail_log(run, timezone.localdate())
        self.assertIsNotNone(log_id)
        site_log = GeezScrapeLog.objects.get()
        self.assertEqual(len(site_log.scraped_log), 1)
        self.assertEqual(site_log.api_hits, 2)
        # The master bucket references the right table.
        master = self.scraper._update_master_day_log(run, timezone.localdate(), log_id)
        bucket = master.website("geezjobs")
        self.assertEqual(bucket["table"], "GeezScrapeLog")
        self.assertEqual(bucket["log_id"], log_id)
        self.assertEqual(bucket["status"], "success")

    def test_sweep_stops_at_today_boundary(self):
        # Page 0 carries today's listing (pre-today items are already dropped
        # inside _page_items by _keep_item); page 1 is entirely pre-today, so
        # its kept list is empty and the sweep must stop there — storing only
        # the single today item and its detail row, and logging the run.
        today_item = self.scraper.normalize(GEEZJOBS_SAMPLE)
        today_item["external_id"] = "today-1"
        today_item["raw_data"] = {**GEEZJOBS_SAMPLE, "slug": "today-1"}
        pages = [
            ([today_item], 200),  # page 0: today's listing
            ([], 200),            # page 1: nothing kept -> stop
        ]
        with mock.patch.object(self.scraper, "_page_items", side_effect=pages):
            self.scraper.scrape_many()

        self.assertTrue(ScrapedItem.objects.filter(external_id="today-1").exists())
        self.assertEqual(GeezJob.objects.count(), 1)
        self.assertEqual(GeezJob.objects.get().job_number, 1)
        # The run landed in the per-site day log for the master rollup.
        site_log = GeezScrapeLog.objects.get(source=self.source)
        self.assertEqual(site_log.run_count, 1)
        self.assertEqual(site_log.items_inserted, 1)

    def test_factory_dispatches_geezjobs_by_slug(self):
        from core.scrapers import ScraperFactory

        scraper = ScraperFactory.for_source(self.source)
        self.assertIsInstance(scraper, GeezJobsScraper)
        self.assertIsInstance(scraper, HtmlScraper)


class ReporterJobsScraperTests(TestCase):
    """The Ethiopian Reporter Jobs HTML scraper: card parsing, exact dates, today filter, detail row."""

    def setUp(self):
        self.source = Source.objects.create(
            slug="reporterjobs",
            name="Ethiopian Reporter Jobs",
            base_url="https://www.ethiopianreporterjobs.com",
            endpoint="https://www.ethiopianreporterjobs.com/jobs-in-ethiopia/",
            scraper_type="html",
            only_today=True,
            field_mapping={
                "external_id": "post_id",
                "title": "title",
                "company": "company",
                "location": "location",
                "job_type": {"path": "job_type", "transforms": ["upper"]},
                "url": "url",
                "published_at": {"path": "published_at", "transforms": ["parse_datetime"]},
                "deadline": {"path": "deadline", "transforms": ["parse_datetime"]},
            },
            pagination={
                "page_size": 10,
                "page_1_based": True,
                "page_style": "path",
                "date_filter": {"field": "published_at"},
                "max_pages": 100,
            },
        )
        self.scraper = ReporterJobsScraper(self.source)

    def test_extract_structure_matches_reporterjobs_sample(self):
        self.assertEqual(extract_structure(REPORTER_SAMPLE), REPORTER_SAMPLE_PATHS)

    def test_reporterjobs_snapshot_exists_and_contains_core_structure(self):
        snapshot = load_structure("reporterjobs")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["source"], "reporterjobs")
        missing = set(REPORTER_SAMPLE_PATHS) - set(snapshot["fields"])
        self.assertFalse(missing, f"Reporter Jobs snapshot is missing core fields: {sorted(missing)}")

    def test_page_url_uses_wordpress_path_pagination(self):
        # Page 1 is the bare /jobs-in-ethiopia/ URL; /page/N/ starts at page 2.
        self.assertEqual(
            self.scraper._page_url(0),
            "https://www.ethiopianreporterjobs.com/jobs-in-ethiopia/",
        )
        self.assertEqual(
            self.scraper._page_url(1),
            "https://www.ethiopianreporterjobs.com/jobs-in-ethiopia/page/2/",
        )
        self.assertEqual(
            self.scraper._page_url(6),
            "https://www.ethiopianreporterjobs.com/jobs-in-ethiopia/page/7/",
        )

    def test_parse_extracts_cards_from_html(self):
        from bs4 import BeautifulSoup

        raw = BeautifulSoup(REPORTER_SAMPLE_HTML, "html.parser")
        items = self.scraper.parse(raw)
        self.assertEqual(len(items), 2)

        first = items[0]
        self.assertEqual(first["post_id"], "284574")
        self.assertEqual(first["title"], "Property Administrator")
        self.assertEqual(
            first["url"],
            "https://www.ethiopianreporterjobs.com/jobs-in-ethiopia/284574/",
        )
        self.assertEqual(first["company"], "LANCET GENERAL HOSPITAL")
        self.assertEqual(first["job_type_text"], "Full Time")
        self.assertEqual(first["job_type"], "full_time")
        self.assertEqual(first["location"], "Addis Ababa")
        self.assertEqual(first["posted_text"], "August 7, 2026")
        self.assertEqual(first["deadline_text"], "August 14, 2026")
        # Exact timestamp from the <time datetime> attribute.
        self.assertEqual(
            first["published_at"], "2026-08-07T06:00:53+03:00"
        )
        # Parsed deadline = local midnight; build the expected offset from the
        # active timezone so the assertion survives a TIME_ZONE change.
        self.assertEqual(
            first["deadline"],
            timezone.make_aware(datetime(2026, 8, 14)).isoformat(),
        )

        second = items[1]
        self.assertEqual(second["title"], "Office Engineer")
        self.assertEqual(second["job_type"], "contract")
        self.assertEqual(second["location"], "Bahir Dar")
        self.assertEqual(
            second["deadline"],
            timezone.make_aware(datetime(2026, 8, 12)).isoformat(),
        )

    def test_shared_month_day_year_parser(self):
        from core.scrapers.html import parse_month_day_year

        # Leading dash (the card's closing span) and 'Deadline:' prefixes are
        # both skipped; full and abbreviated month names both work.
        self.assertEqual(
            parse_month_day_year("- August 12, 2026"),
            timezone.make_aware(datetime(2026, 8, 12)),
        )
        self.assertEqual(
            parse_month_day_year("Deadline: Sep 7, 2026"),
            timezone.make_aware(datetime(2026, 9, 7)),
        )
        self.assertIsNone(parse_month_day_year("nope"))
        self.assertIsNone(parse_month_day_year(""))

    def test_normalize_maps_card_fields(self):
        item = self.scraper.normalize(REPORTER_SAMPLE)
        self.assertEqual(item["external_id"], "284574")
        self.assertEqual(item["title"], "Property Administrator")
        self.assertEqual(item["company"], "LANCET GENERAL HOSPITAL")
        self.assertEqual(item["location"], "Addis Ababa")
        # The site's normalized type value maps to the shared enum.
        self.assertEqual(item["job_type"], "FULL_TIME")
        self.assertIsNotNone(item["published_at"])
        self.assertIsNotNone(item["deadline"])

    def test_parse_raises_on_challenge_page(self):
        from bs4 import BeautifulSoup

        # No cards AND no archive container -> Cloudflare challenge / error page.
        blocked = BeautifulSoup(
            '<html><body><title>Attention Required! | Cloudflare</title>'
            '<div class="cf-error-details"></div></body></html>',
            "html.parser",
        )
        with self.assertRaises(ScrapeError) as ctx:
            self.scraper.parse(blocked)
        self.assertIn("Cloudflare", str(ctx.exception))

    def test_parse_returns_empty_on_genuine_empty_page(self):
        from bs4 import BeautifulSoup

        # Real listings page (archive container present) but zero cards: legit
        # empty (e.g. WordPress redirected the deep page to /expired).
        empty = BeautifulSoup(
            '<html><body><div class="jobs posts-loop"></div></body></html>',
            "html.parser",
        )
        self.assertEqual(self.scraper.parse(empty), [])

    def test_parse_returns_empty_on_expired_end_of_feed_page(self):
        from bs4 import BeautifulSoup

        # Deep archive pages redirect to /expired: site framing (header) is
        # intact but there are no cards and no archive container. That must be
        # a clean end-of-feed (so a backfill sweep stops), NOT a ScrapeError.
        expired = BeautifulSoup(
            '<html><body><header><nav><a href="/">Home</a></nav></header>'
            '<h1>Expired</h1><p>These jobs have expired.</p></body></html>',
            "html.parser",
        )
        self.assertEqual(self.scraper.parse(expired), [])

    def test_normalize_job_type_maps_known_and_drops_unknown(self):
        from core.scrapers.reporterjobs import _normalize_job_type

        # Known chips map to the shared JobType values.
        self.assertEqual(_normalize_job_type("Full Time"), "full_time")
        self.assertEqual(_normalize_job_type("Contract"), "contract")
        self.assertEqual(_normalize_job_type("Part Time"), "part_time")
        self.assertEqual(_normalize_job_type("Internship"), "internship")
        # Parenthetical qualifiers are stripped before mapping.
        self.assertEqual(_normalize_job_type("Full Time (Remote)"), "full_time")
        # Unknown phrases map to '' so the master enum never gets polluted
        # (the raw text is still kept on ReporterJob.job_type_text).
        self.assertEqual(_normalize_job_type("Unicorn Position"), "")
        self.assertEqual(_normalize_job_type(""), "")

    @staticmethod
    def _item(days_ago=0):
        """A normalized Reporter Jobs item published ``days_ago`` days from now."""
        return {
            "external_id": "post-%d" % days_ago,
            "title": "Job %d" % days_ago,
            "location": "Addis Ababa",
            "job_type": "FULL_TIME",
            "published_at": timezone.now() - timedelta(days=days_ago),
            "raw_data": {"post_id": "post-%d" % days_ago},
        }

    def test_keep_item_drops_pre_today(self):
        self.assertTrue(self.scraper._keep_item(self._item(0)))
        self.assertFalse(self.scraper._keep_item(self._item(1)))
        self.assertFalse(self.scraper._keep_item(self._item(3)))

    def test_past_today_boundary(self):
        # A page whose kept items are all pre-today marks the boundary.
        self.assertTrue(
            self.scraper._past_today_boundary(2, [self._item(1), self._item(3)])
        )
        # A page with today's items (even mixed) does not.
        self.assertFalse(
            self.scraper._past_today_boundary(0, [self._item(0), self._item(1)])
        )

    def test_save_detail_persists_everything_and_links_master(self):
        item = self.scraper.normalize(REPORTER_SAMPLE)
        item["raw_data"] = REPORTER_SAMPLE
        master_item = ScrapedItem.objects.create(
            source=self.source,
            external_id=item["external_id"],
            title=item["title"],
            job_number=1,
            numbered_on=timezone.localdate(),
        )
        self.scraper._save_detail(item, master_item)
        detail = ReporterJob.objects.get(external_id=item["external_id"])
        self.assertEqual(detail.company, "LANCET GENERAL HOSPITAL")
        self.assertEqual(detail.location, "Addis Ababa")
        self.assertEqual(detail.job_type_text, "Full Time")
        self.assertEqual(detail.job_type, "full_time")
        self.assertEqual(detail.job_type_display, "Full Time")
        self.assertEqual(detail.posted_text, "August 7, 2026")
        self.assertEqual(detail.deadline_text, "August 14, 2026")
        self.assertIsNotNone(detail.published_at)
        self.assertIsNotNone(detail.deadline)
        self.assertEqual(detail.job_number, 1)
        master_item.refresh_from_db()
        self.assertEqual(master_item.reporter_job_id, detail.pk)

    def test_generic_record_detail_log_writes_reporterjobs_site_log(self):
        run = make_run(api_hits=2, found=3)
        log_id = self.scraper.record_detail_log(run, timezone.localdate())
        self.assertIsNotNone(log_id)
        site_log = ReporterScrapeLog.objects.get()
        self.assertEqual(len(site_log.scraped_log), 1)
        self.assertEqual(site_log.api_hits, 2)
        # The master bucket references the right table.
        master = self.scraper._update_master_day_log(run, timezone.localdate(), log_id)
        bucket = master.website("reporterjobs")
        self.assertEqual(bucket["table"], "ReporterScrapeLog")
        self.assertEqual(bucket["log_id"], log_id)
        self.assertEqual(bucket["status"], "success")

    def test_sweep_stops_at_today_boundary(self):
        # Page 0 carries today's listing (pre-today items are already dropped
        # inside _page_items by _keep_item); page 1 is entirely pre-today, so
        # its kept list is empty and the sweep must stop there — storing only
        # the single today item and its detail row, and logging the run.
        today_item = self.scraper.normalize(REPORTER_SAMPLE)
        today_item["external_id"] = "today-1"
        today_item["raw_data"] = {**REPORTER_SAMPLE, "post_id": "today-1"}
        pages = [
            ([today_item], 200),  # page 0: today's listing
            ([], 200),            # page 1: nothing kept -> stop
        ]
        with mock.patch.object(self.scraper, "_page_items", side_effect=pages):
            self.scraper.scrape_many()

        self.assertTrue(ScrapedItem.objects.filter(external_id="today-1").exists())
        self.assertEqual(ReporterJob.objects.count(), 1)
        self.assertEqual(ReporterJob.objects.get().job_number, 1)
        # The run landed in the per-site day log for the master rollup.
        site_log = ReporterScrapeLog.objects.get(source=self.source)
        self.assertEqual(site_log.run_count, 1)
        self.assertEqual(site_log.items_inserted, 1)

    def test_factory_dispatches_reporterjobs_by_slug(self):
        from core.scrapers import ScraperFactory

        scraper = ScraperFactory.for_source(self.source)
        self.assertIsInstance(scraper, ReporterJobsScraper)
        self.assertIsInstance(scraper, HtmlScraper)


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

    def test_issue_reported_from_second_website(self):
        # A non-200 from EthioJobs must be attributed to EthioJobs.
        ej_source = Source.objects.create(
            slug="ethiojobs", name="EthioJobs", endpoint="https://example.com/jobs"
        )
        run = make_run(api_hits=1, found=0)
        run["pages_hit"] = [{"page": 0, "http_status": 503, "found": 0}]
        EthioJobsScrapeLog.objects.create(
            source=ej_source,
            day=self.today,
            status="failed",
            run_count=1,
            api_hits=1,
            items_found=0,
            items_inserted=0,
            items_updated=0,
            items_skipped=0,
            scraped_log=[run],
        )
        issues = api_issues_for_day(self.today)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["website"], "ethiojobs")
        self.assertEqual(issues[0]["table"], "EthioJobsScrapeLog")
        self.assertEqual(issues[0]["http_status"], 503)

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


class ScrapeAllCommandTests(TestCase):
    """The one-command scrape_all: calls scrape_source for every active source."""

    def test_scrape_all_calls_each_active_source_and_continues_on_failure(self):
        Source.objects.create(
            slug="afriwork", name="Afriwork", endpoint="https://example.com/1",
            is_active=True,
        )
        Source.objects.create(
            slug="ethiojobs", name="EthioJobs", endpoint="https://example.com/2",
            is_active=False,  # inactive sources are skipped
        )
        Source.objects.create(
            slug="geezjobs", name="GeezJobs", endpoint="https://example.com/3",
            is_active=True,
        )

        calls: list[tuple] = []

        def fake_call(command, *args, **kwargs):
            calls.append((command, args[0]))
            if args[0] == "geezjobs":
                raise RuntimeError("boom")

        out = io.StringIO()
        with mock.patch(
            "core.management.commands.scrape_all.call_command", side_effect=fake_call
        ):
            with self.assertRaises(SystemExit):
                call_command("scrape_all", stdout=out)

        # Every active source ran in order; the inactive one was skipped, and
        # one failure did not stop the others.
        self.assertEqual(
            calls,
            [("scrape_source", "afriwork"), ("scrape_source", "geezjobs")],
        )
        text = out.getvalue()
        self.assertIn("1 source(s) failed", text)
        self.assertIn("geezjobs: failed: boom", text)

    def test_scrape_all_passes_options_and_slug_filter_through(self):
        Source.objects.create(
            slug="afriwork", name="Afriwork", endpoint="https://example.com/1",
            is_active=True,
        )
        Source.objects.create(
            slug="geezjobs", name="GeezJobs", endpoint="https://example.com/3",
            is_active=True,
        )

        with mock.patch(
            "core.management.commands.scrape_all.call_command", return_value=None
        ) as fake_call:
            call_command(
                "scrape_all",
                no_today=True,
                page=2,
                slugs=["afriwork"],
                stdout=io.StringIO(),
            )

        # --slugs filtered to afriwork; --no-today and --page passed through.
        fake_call.assert_called_once_with(
            "scrape_source", "afriwork", no_today=True, page=2
        )

    def test_scrape_all_warns_without_active_sources(self):
        out = io.StringIO()
        call_command("scrape_all", stdout=out)
        self.assertIn("No active sources", out.getvalue())

    def test_scrape_all_rejects_unknown_slugs(self):
        Source.objects.create(
            slug="afriwork", name="Afriwork", endpoint="https://example.com/1",
            is_active=True,
        )
        out = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("scrape_all", slugs=["afriwork", "nope"], stdout=out)

    def test_scrape_all_skips_requested_inactive_sources_with_warning(self):
        Source.objects.create(
            slug="afriwork", name="Afriwork", endpoint="https://example.com/1",
            is_active=True,
        )
        Source.objects.create(
            slug="ethiojobs", name="EthioJobs", endpoint="https://example.com/2",
            is_active=False,
        )

        with mock.patch(
            "core.management.commands.scrape_all.call_command", return_value=None
        ) as fake_call:
            out = io.StringIO()
            call_command("scrape_all", slugs=["afriwork", "ethiojobs"], stdout=out)

        # The active source was scraped; the inactive one was skipped with a
        # warning (and its slug never reached scrape_source).
        fake_call.assert_called_once_with("scrape_source", "afriwork")
        self.assertIn("inactive", out.getvalue())
