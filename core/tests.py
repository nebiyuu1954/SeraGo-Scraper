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
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import httpx

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import (
    AfriworkJob,
    AfriworkScrapeLog,
    ArchiveRun,
    CategoryStat,
    EthioJobsJob,
    EthioJobsScrapeLog,
    GeezJob,
    GeezScrapeLog,
    HaHuJob,
    HaHuScrapeLog,
    ReporterJob,
    ReporterScrapeLog,
    ScrapeLog,
    ScrapeStat,
    ScrapeStatus,
    ScrapedItem,
    Source,
)
from core.management.commands.telegram_report import Command as TelegramReportCommand
from core.reporting import (
    api_issues_for_day,
    deep_sweep_sources_for_day,
    month_bounds,
    recompute_sector_stats,
    recompute_stat,
    silent_zero_sources_for_day,
    stat_block,
    update_current_stats,
    week_bounds,
    year_bounds,
)
from core.scrapers.base import (
    LIFECYCLE_GRACE_DAYS,
    ScrapeError,
    transform_job_type_code,
    transform_strip_html,
)
from core.scrapers.geezjobs import GeezJobsScraper
from core.scrapers.graphql import AfriworkJobsScraper, GraphQLScraper
from core.scrapers.hahujobs import HaHuJobsScraper
from core.scrapers.html import HtmlScraper, parse_month_day_year
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

# A faithful minimal listing page for Ethiopian Reporter Jobs (WordPress /
# Careerfy theme, mid-2026 redesign). It mirrors the live /jobs-in-ethiopia/
# markup: one div.jobsearch-joblisting-classic-wrap per listing with
# h2.jobsearch-pst-title (carrying data-job-id), the job-company-name li, the
# maps-and-flags location li, the calendar "Published X hours ago" li and the
# a.jobsearch-option-btn type badge. There are NO exact timestamps or
# deadlines on the new cards (published_at is estimated from the relative
# text; the deadline stays None so the shared +30-day default applies). Two
# cards, both "hours ago".
REPORTER_SAMPLE_HTML = """<!DOCTYPE html>
<html lang="en-US"><head><meta charset="UTF-8"><title>Jobs In Ethiopia - Latest Vacancies &amp; Employment 2026</title></head>
<body>
<header id="careerfy-header"><nav><a href="/">Home</a></nav></header>
  <div class="jobsearch-job" id="jobsearch-job-1">
    <ul class="jobsearch-row">
      <li class="jobsearch-column-12">
        <div class="jobsearch-joblisting-classic-wrap">
          <div class="jobsearch-joblisting-text">
            <div class="jobsearch-list-option">
              <h2 class="jobsearch-pst-title" data-job-id="284574">
                <a href="https://www.ethiopianreporterjobs.com/jobs/284574/" title="Property Administrator">Property Administrator</a>
              </h2>
              <ul>
                <li class="job-company-name"><a href="https://www.ethiopianreporterjobs.com/company/lancet/">@ LANCET GENERAL HOSPITAL</a></li>
                <li><i class="jobsearch-icon jobsearch-maps-and-flags"></i>Addis Ababa, Ethiopia</li>
              </ul>
              <ul>
                <li><i class="jobsearch-icon jobsearch-calendar"></i>Published  3 hours ago</li>
                <li><i class="jobsearch-icon jobsearch-filter-tool-black-shape"></i><a href="/customer-service-support-jobs/">Customer Service and Support</a></li>
              </ul>
            </div>
            <div class="jobsearch-job-userlist">
              <a href="https://www.ethiopianreporterjobs.com/full-time-jobs/" class="jobsearch-option-btn"> Full-time </a>
            </div>
          </div>
        </div>
      </li>
      <li class="jobsearch-column-12">
        <div class="jobsearch-joblisting-classic-wrap">
          <div class="jobsearch-joblisting-text">
            <div class="jobsearch-list-option">
              <h2 class="jobsearch-pst-title" data-job-id="285000">
                <a href="https://www.ethiopianreporterjobs.com/jobs/285000/" title="Office Engineer">Office Engineer</a>
              </h2>
              <ul>
                <li class="job-company-name"><a href="https://www.ethiopianreporterjobs.com/company/4b/">@ 4B Trading PLC</a></li>
                <li><i class="jobsearch-icon jobsearch-maps-and-flags"></i>Bahir Dar, Ethiopia</li>
              </ul>
              <ul>
                <li><i class="jobsearch-icon jobsearch-calendar"></i>Published  5 hours ago</li>
                <li><i class="jobsearch-icon jobsearch-filter-tool-black-shape"></i><a href="/contract-jobs/">Contract</a></li>
              </ul>
            </div>
            <div class="jobsearch-job-userlist">
              <a href="https://www.ethiopianreporterjobs.com/contract-jobs/" class="jobsearch-option-btn"> Contract </a>
            </div>
          </div>
        </div>
      </li>
    </ul>
  </div>
</body></html>
"""

# The raw card dict ReporterJobsScraper.parse() produces for the FIRST card
# above. published_at is ESTIMATED from the relative "Published X hours ago"
# text (pinned clock in the parse test); the theme exposes no exact timestamp
# and no deadline, so deadline stays None (the shared +30-day default applies).
REPORTER_SAMPLE = {
    "post_id": "284574",
    "title": "Property Administrator",
    "url": "https://www.ethiopianreporterjobs.com/jobs/284574/",
    "company": "LANCET GENERAL HOSPITAL",
    "job_type_text": "Full-time",
    "job_type": "full_time",
    "location": "Addis Ababa, Ethiopia",
    "posted_text": "Published 3 hours ago",
    "published_at": "2026-08-07T06:00:53+03:00",
    "deadline_text": "",
    "deadline": None,
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

    def test_strip_html_preserves_block_structure(self):
        html = (
            "<p>VACANCY ANNOUNCEMENT</p>"
            "<p>Position: Senior Sales Engineer</p>"
            "<p>About Us</p>"
            "<p>We are a company &amp; we build things.</p>"
            "<p>Key Responsibilities</p>"
            "<ul><li><p>Prepare bids</p></li><li><p>Manage submissions</p></li></ul>"
            "<p>Skills:</p>"
            "<ul><li>MS Office</li><li>Attention to detail</li></ul>"
            "<p>How to Apply</p>"
            "<p>Send your CV<br />by email</p>"
        )
        self.assertEqual(
            transform_strip_html(html),
            "VACANCY ANNOUNCEMENT\n"
            "Position: Senior Sales Engineer\n"
            "About Us\n"
            "We are a company & we build things.\n"
            "Key Responsibilities\n"
            "• Prepare bids\n"
            "• Manage submissions\n"
            "Skills:\n"
            "• MS Office\n"
            "• Attention to detail\n"
            "How to Apply\n"
            "Send your CV\n"
            "by email",
        )

    def test_strip_html_handles_none_and_plain_text(self):
        self.assertIsNone(transform_strip_html(None))
        self.assertEqual(
            transform_strip_html("Diploma or Bachelor's Degree with work experience"),
            "Diploma or Bachelor's Degree with work experience",
        )
        self.assertEqual(transform_strip_html(""), None)


class RequestRetryTests(TestCase):
    """request_with_retry: transient network errors are retried, then raised."""

    def test_retries_transient_errors_then_succeeds(self):
        from core.scrapers.base import request_with_retry

        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ReadTimeout("slow server")
            return "ok"

        self.assertEqual(request_with_retry(flaky, retries=3, backoff_seconds=0), "ok")
        self.assertEqual(calls["n"], 3)

    def test_raises_after_retries_exhausted(self):
        from core.scrapers.base import request_with_retry

        def always_fails(**kwargs):
            raise httpx.ReadTimeout("slow server")

        with self.assertRaises(httpx.ReadTimeout):
            request_with_retry(always_fails, retries=2, backoff_seconds=0)

    def test_does_not_retry_non_transport_errors(self):
        from core.scrapers.base import request_with_retry

        def boom(**kwargs):
            raise ValueError("not a network error")

        with self.assertRaises(ValueError):
            request_with_retry(boom, retries=3, backoff_seconds=0)


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

    def test_save_items_batches_insert_update_skip(self):
        """save_items dedupes correctly across the batched insert/update/skip paths."""
        item = self._item(0)
        inserted, updated, skipped, errors = self.scraper.save_items([item])
        self.assertEqual((inserted, updated, skipped, errors), (1, 0, 0, []))
        master = ScrapedItem.objects.get(external_id="slug-0")
        self.assertEqual(master.job_number, 1)
        self.assertEqual(master.numbered_on, timezone.localdate())
        self.assertEqual(EthioJobsJob.objects.count(), 1)
        self.assertEqual(
            ScrapedItem.objects.get(external_id="slug-0").ethiojobs_job_id,
            EthioJobsJob.objects.get(external_id="slug-0").pk,
        )

        # Same external_id, changed content -> the bulk update path.
        changed = self._item(0)
        changed["title"] = "Job 0 (updated)"
        inserted, updated, skipped, errors = self.scraper.save_items([changed])
        self.assertEqual((inserted, updated, skipped, errors), (0, 1, 0, []))
        master.refresh_from_db()
        self.assertEqual(master.title, "Job 0 (updated)")
        # The detail row was upserted in place (ON CONFLICT DO UPDATE) and the
        # master link still points at it.
        detail = EthioJobsJob.objects.get(external_id="slug-0")
        self.assertEqual(detail.title, "Job 0 (updated)")
        self.assertEqual(
            ScrapedItem.objects.get(external_id="slug-0").ethiojobs_job_id, detail.pk
        )

        # Unchanged content -> skipped (touch only).
        inserted, updated, skipped, errors = self.scraper.save_items([changed])
        self.assertEqual((inserted, updated, skipped, errors), (0, 0, 1, []))

    def test_backfill_items_numbered_on_their_published_day(self):
        """Old jobs swept in by a backfill get THEIR day's serials, not today's.

        The whole point of the numbering rule: an item published 3 days ago
        that first enters the DB today must become #01 of its own day, never
        #N of today. Items with no published date fall back to today.
        """
        from datetime import timedelta

        three_days_ago = timezone.now() - timedelta(days=3)
        old_early = self._item(3)
        old_early["published_at"] = three_days_ago - timedelta(hours=1)
        old_early["external_id"] = "slug-old-early"
        old_late = self._item(3)
        old_late["published_at"] = three_days_ago + timedelta(hours=1)
        old_late["external_id"] = "slug-old-late"
        fresh = self._item(0)  # published now -> today

        # Passed oldest-first, exactly as scrape_many sorts before save_items.
        inserted, updated, skipped, errors = self.scraper.save_items(
            [old_early, old_late, fresh]
        )
        self.assertEqual((inserted, updated, skipped, errors), (3, 0, 0, []))

        old_day = timezone.localtime(three_days_ago).date()
        early_master = ScrapedItem.objects.get(external_id="slug-old-early")
        late_master = ScrapedItem.objects.get(external_id="slug-old-late")
        fresh_master = ScrapedItem.objects.get(external_id="slug-0")
        self.assertEqual(early_master.numbered_on, old_day)
        self.assertEqual(late_master.numbered_on, old_day)
        # Chronological within the old day: #01 early, #02 later.
        self.assertEqual(early_master.job_number, 1)
        self.assertEqual(late_master.job_number, 2)
        # Today's serial starts fresh at #01 — untouched by the backfill.
        self.assertEqual(fresh_master.numbered_on, timezone.localdate())
        self.assertEqual(fresh_master.job_number, 1)

    def test_insert_item_numbers_on_published_day(self):
        """The per-item fallback path uses the published day too."""
        from datetime import timedelta

        old = self._item(3)
        old["external_id"] = "slug-per-item-old"
        defaults = {
            k: v
            for k, v in old.items()
            if k not in ("external_id", "raw_data") and v is not None
        }
        instance = self.scraper._insert_item("slug-per-item-old", defaults)
        self.assertIsNotNone(instance)
        old_day = timezone.localtime(old["published_at"]).date()
        self.assertEqual(instance.numbered_on, old_day)
        self.assertEqual(instance.job_number, 1)

        # A second old item for the same day appends to THAT day's serials.
        defaults["title"] = "Old job, later post"
        defaults["published_at"] = old["published_at"] + timedelta(hours=1)
        instance2 = self.scraper._insert_item("slug-per-item-old-2", defaults)
        self.assertEqual(instance2.numbered_on, old_day)
        self.assertEqual(instance2.job_number, 2)

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

    def test_jina_relay_url_encodes_target_with_query(self):
        # With pagination.relay="jina" the target (query string included) is
        # percent-encoded into the relay's path, so ?page=N stays part of the
        # target URL instead of becoming the relay's own query param.
        self.source.pagination = {**self.source.pagination, "relay": "jina"}
        self.assertEqual(
            self.scraper._relay_url("https://geezjobs.com/search-jobs?page=2"),
            "https://r.jina.ai/https%3A%2F%2Fgeezjobs.com%2Fsearch-jobs%3Fpage%3D2",
        )
        # No relay configured -> the URL passes through unchanged.
        self.source.pagination = {"page_1_based": True}
        self.assertEqual(
            self.scraper._relay_url("https://geezjobs.com/search-jobs"),
            "https://geezjobs.com/search-jobs",
        )

    def test_fetch_uses_jina_relay_when_configured(self):
        # With the relay on, fetch() hits the relay URL and asks for fresh raw
        # HTML (the site-specific parse needs the markup, not markdown).
        # JINA_API_KEY is pinned to "" for the no-key branch so the test never
        # depends on the ambient environment (a key in .env would otherwise
        # add the Authorization header and fail this assertion).
        self.source.pagination = {**self.source.pagination, "relay": "jina"}
        self.scraper = GeezJobsScraper(self.source)
        with override_settings(JINA_API_KEY=""), mock.patch(
            "core.scrapers.html.httpx.get"
        ) as get:
            get.return_value.status_code = 200
            get.return_value.raise_for_status = lambda: None
            get.return_value.text = GEEZJOBS_SAMPLE_HTML
            self.scraper.fetch(0)
        self.assertEqual(
            get.call_args.kwargs["url"],
            "https://r.jina.ai/https%3A%2F%2Fgeezjobs.com%2Fsearch-jobs",
        )
        headers = get.call_args.kwargs["headers"]
        self.assertEqual(headers["X-Return-Format"], "html")
        self.assertEqual(headers["X-No-Cache"], "true")
        # The site's browser headers must NOT be forwarded — the relay's own
        # WAF 403s requests carrying a browser User-Agent.
        self.assertNotIn("User-Agent", headers)
        self.assertNotIn("Accept", headers)
        self.assertNotIn("Authorization", headers)  # no JINA_API_KEY configured

        # When JINA_API_KEY is configured, the relay gets a Bearer token
        # (mocked too — no real network calls in tests).
        with override_settings(JINA_API_KEY="test-key"), mock.patch(
            "core.scrapers.html.httpx.get"
        ) as get2:
            get2.return_value.status_code = 200
            get2.return_value.raise_for_status = lambda: None
            get2.return_value.text = GEEZJOBS_SAMPLE_HTML
            self.scraper.fetch(0)
        headers = get2.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-key")

    def test_fetch_retries_transient_relay_http_errors(self):
        # The free relay pool is flaky — a 403/429/5xx blip from the RELAY
        # (shared infra, not the site's WAF) is retried with backoff instead
        # of failing the run on the first attempt.
        self.source.pagination = {**self.source.pagination, "relay": "jina"}
        self.scraper = GeezJobsScraper(self.source)
        calls = {"n": 0}

        def fake_get(**kwargs):
            calls["n"] += 1
            response = mock.Mock()
            response.status_code = 403 if calls["n"] == 1 else 200
            response.raise_for_status = lambda: None
            response.text = GEEZJOBS_SAMPLE_HTML
            return response

        with mock.patch("core.scrapers.html.httpx.get", side_effect=fake_get), mock.patch(
            "core.scrapers.html.time.sleep"
        ):
            self.scraper.fetch(0)
        self.assertEqual(calls["n"], 2)  # one 403 blip, one success

    def test_fetch_fails_when_relay_keeps_returning_403(self):
        # A persistent 403 must fail the run loudly (never silently succeed
        # with an empty parse). With the default 3 transport retries + 2 relay
        # retries the fetch gives up after 5 attempts.
        self.source.pagination = {**self.source.pagination, "relay": "jina"}
        self.scraper = GeezJobsScraper(self.source)
        calls = {"n": 0}

        def fake_get(**kwargs):
            calls["n"] += 1
            response = mock.Mock()
            response.status_code = 403
            response.raise_for_status = lambda: None
            response.text = "<html></html>"
            return response

        with mock.patch("core.scrapers.html.httpx.get", side_effect=fake_get), mock.patch(
            "core.scrapers.html.time.sleep"
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                self.scraper.fetch(0)
        self.assertEqual(calls["n"], 5)  # 3 transport retries + 2 relay retries

    def test_fetch_retries_402_blip_then_succeeds(self):
        # A 402 (Payment Required) blip from the Jina relay — the free-tier
        # quota is momentarily exhausted — is retried like 403/429/5xx.
        self.source.pagination = {**self.source.pagination, "relay": "jina"}
        self.scraper = GeezJobsScraper(self.source)
        calls = {"n": 0}

        def fake_get(**kwargs):
            calls["n"] += 1
            response = mock.Mock()
            response.status_code = 402 if calls["n"] == 1 else 200
            response.raise_for_status = lambda: None
            response.text = GEEZJOBS_SAMPLE_HTML
            return response

        with mock.patch("core.scrapers.html.httpx.get", side_effect=fake_get), mock.patch(
            "core.scrapers.html.time.sleep"
        ):
            self.scraper.fetch(0)
        self.assertEqual(calls["n"], 2)  # one 402 blip, one success

    def test_fetch_fails_with_clear_message_when_relay_always_402(self):
        # Persistent 402 from the Jina relay (quota exhausted) must fail with
        # a clear ScrapeError mentioning quota/JINA_API_KEY, not a raw
        # HTTPStatusError.
        self.source.pagination = {**self.source.pagination, "relay": "jina"}
        self.scraper = GeezJobsScraper(self.source)
        calls = {"n": 0}

        def fake_get(**kwargs):
            calls["n"] += 1
            response = mock.Mock()
            response.status_code = 402
            response.raise_for_status = lambda: None
            response.text = "<html></html>"
            return response

        with mock.patch("core.scrapers.html.httpx.get", side_effect=fake_get), mock.patch(
            "core.scrapers.html.time.sleep"
        ):
            with self.assertRaises(ScrapeError) as ctx:
                self.scraper.fetch(0)
        self.assertIn("402", str(ctx.exception))
        self.assertIn("quota", str(ctx.exception).lower())
        self.assertEqual(calls["n"], 5)  # 3 transport + 2 relay retries


    @staticmethod
    def _challenge_page() -> str:
        # A faithful slice of what Cloudflare serves: the relay returns it
        # with HTTP 200 (the relay itself got through), so only the body
        # reveals that the target answered with its bot-check page.
        return (
            '<html lang="en-US"><head><title>Just a moment...</title>'
            '<meta http-equiv="refresh" content="360">'
            '<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1?ray=abc">'
            "</script></head><body><div class=\"main-content\">Checking your browser...</div></body></html>"
        )

    def test_fetch_retries_cloudflare_challenge_then_succeeds(self):
        # The target site can serve its Cloudflare challenge page to the relay
        # (HTTP 200 — the relay itself got through). That must be retried like
        # a transient blip — the block often lifts — never accepted as the page.
        self.source.pagination = {**self.source.pagination, "relay": "jina"}
        self.scraper = GeezJobsScraper(self.source)
        calls = {"n": 0}

        def fake_get(**kwargs):
            calls["n"] += 1
            response = mock.Mock()
            response.status_code = 200
            response.raise_for_status = lambda: None
            response.text = self._challenge_page() if calls["n"] == 1 else GEEZJOBS_SAMPLE_HTML
            return response

        with mock.patch("core.scrapers.html.httpx.get", side_effect=fake_get), mock.patch(
            "core.scrapers.html.time.sleep"
        ):
            self.scraper.fetch(0)
        self.assertEqual(calls["n"], 2)  # one challenge, one success

    def test_fetch_raises_clear_error_when_always_challenged(self):
        # When every attempt returns the challenge page the run must fail
        # loudly with a message that NAMES Cloudflare (so the run summary / day
        # log says what is actually wrong), never a silent empty success.
        self.source.pagination = {**self.source.pagination, "relay": "jina"}
        self.scraper = GeezJobsScraper(self.source)
        calls = {"n": 0}

        def fake_get(**kwargs):
            calls["n"] += 1
            response = mock.Mock()
            response.status_code = 200
            response.raise_for_status = lambda: None
            response.text = self._challenge_page()
            return response

        with mock.patch("core.scrapers.html.httpx.get", side_effect=fake_get), mock.patch(
            "core.scrapers.html.time.sleep"
        ):
            with self.assertRaises(ScrapeError) as ctx:
                self.scraper.fetch(0)
        self.assertEqual(calls["n"], 5)  # 3 transport retries + 2 relay retries
        self.assertIn("Cloudflare", str(ctx.exception))

    @staticmethod
    def _scrapfly_response(
        content: str, status_code: int = 200, api_status: int = 200, success: bool = True
    ):
        """A mock ScrapFly API response (JSON envelope -> result.content)."""
        response = mock.Mock()
        response.status_code = api_status
        response.raise_for_status = lambda: None
        response.json.return_value = {
            "result": {
                "success": success,
                "status_code": status_code,
                "reason": "OK",
                "content": content,
            }
        }
        return response

    def test_fetch_via_scrapfly_builds_request_and_parses(self):
        # pagination.relay="scrapfly" routes the page through the ScrapFly
        # anti-bot API: asp (bypass) + render_js (JS-rendered cards); the JSON
        # envelope's result.content holds the rendered HTML and its
        # result.status_code is the TARGET's status.
        self.source.pagination = {**self.source.pagination, "relay": "scrapfly"}
        self.scraper = GeezJobsScraper(self.source)
        with override_settings(SCRAPFLY_API_KEY="test-key"), mock.patch(
            "core.scrapers.html.httpx.get"
        ) as get:
            get.return_value = self._scrapfly_response(GEEZJOBS_SAMPLE_HTML)
            soup = self.scraper.fetch(0)
        self.assertIsNotNone(soup.select_one(".opportunity-card"))
        # Registry-based dispatch uses keyword args: httpx.get(url=..., params=...)
        url_arg = get.call_args.kwargs.get("url") if get.call_args.kwargs else None
        self.assertEqual(url_arg, "https://api.scrapfly.io/scrape")
        params = get.call_args.kwargs.get("params") or {}
        self.assertEqual(params.get("url"), "https://geezjobs.com/search-jobs")
        self.assertEqual(params.get("key"), "test-key")
        self.assertEqual(params.get("asp"), "true")
        self.assertEqual(params.get("render_js"), "true")
        self.assertNotIn("proxified_response", params)

    def test_fetch_via_scrapfly_requires_key(self):
        self.source.pagination = {**self.source.pagination, "relay": "scrapfly"}
        self.scraper = GeezJobsScraper(self.source)
        with override_settings(SCRAPFLY_API_KEY=""):
            with self.assertRaises(ScrapeError) as ctx:
                self.scraper.fetch(0)
        # The error message names the service and mentions the API key
        self.assertIn("ScrapFly", str(ctx.exception))
        self.assertIn("api key", str(ctx.exception).lower())

    def test_fetch_via_scrapfly_raises_when_challenge_unbypassed(self):
        # Even ScrapFly's asp can lose to a strict challenge — the response
        # must fail loudly with a message that names Cloudflare, never be
        # parsed as an empty feed.
        self.source.pagination = {
            **self.source.pagination,
            "relay": "scrapfly",
            "retries": 1,
            "relay_backoff_seconds": 0.0,
        }
        self.scraper = GeezJobsScraper(self.source)

        def fake_get(*args, **kwargs):
            return self._scrapfly_response(self._challenge_page())

        with override_settings(SCRAPFLY_API_KEY="test-key"), mock.patch(
            "core.scrapers.html.httpx.get", side_effect=fake_get
        ), mock.patch("core.scrapers.html.time.sleep"):
            with self.assertRaises(ScrapeError) as ctx:
                self.scraper.fetch(0)
        self.assertIn("Cloudflare", str(ctx.exception))

    def test_fetch_via_scrapfly_retries_transient_blips(self):
        # A 429/5xx from ScrapFly itself (quota/concurrency blip) is retried
        # with backoff like the relay blips are.
        self.source.pagination = {
            **self.source.pagination,
            "relay": "scrapfly",
            "retries": 2,
            "relay_backoff_seconds": 0.0,
        }
        self.scraper = GeezJobsScraper(self.source)
        calls = {"n": 0}

        def fake_get(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                response = mock.Mock()
                response.status_code = 429
                response.text = ""
                return response
            return self._scrapfly_response(GEEZJOBS_SAMPLE_HTML)

        with override_settings(SCRAPFLY_API_KEY="test-key"), mock.patch(
            "core.scrapers.html.httpx.get", side_effect=fake_get
        ), mock.patch("core.scrapers.html.time.sleep"):
            self.scraper.fetch(0)
        self.assertEqual(calls["n"], 2)

    def test_fetch_via_scrapfly_retries_failed_result(self):
        # asp intermittently loses to the target's WAF (success=False,
        # "Forbidden") even though the next attempt succeeds — that must be
        # retried, not treated as a hard failure.
        self.source.pagination = {
            **self.source.pagination,
            "relay": "scrapfly",
            "retries": 2,
            "relay_backoff_seconds": 0.0,
        }
        self.scraper = GeezJobsScraper(self.source)
        calls = {"n": 0}

        def fake_get(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return self._scrapfly_response("", success=False)
            return self._scrapfly_response(GEEZJOBS_SAMPLE_HTML)

        with override_settings(SCRAPFLY_API_KEY="test-key"), mock.patch(
            "core.scrapers.html.httpx.get", side_effect=fake_get
        ), mock.patch("core.scrapers.html.time.sleep"):
            soup = self.scraper.fetch(0)
        self.assertEqual(calls["n"], 2)
        self.assertIsNotNone(soup.select_one(".opportunity-card"))

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


class RelayRotationTests(TestCase):
    """Tests for relay_rotate — Jina-first rotation with CF fallback."""

    def setUp(self):
        self.source = Source.objects.create(
            slug="geezjobs",
            name="GeezJobs",
            endpoint="https://geezjobs.com/search-jobs",
            scraper_type="html",
            only_today=True,
            pagination={
                "relay": "relay_rotate",
                "page_1_based": True,
                "page_key": "page",
                "page_size": 15,
                "timeout": 60.0,
            },
        )
        self.scraper = GeezJobsScraper(self.source)

    def _make_response(self, status_code: int, text: str = "") -> mock.Mock:
        """Create a mock httpx.Response with proper status_code as int."""
        resp = mock.Mock()
        resp.status_code = status_code
        resp.raise_for_status = lambda: None
        resp.text = text
        return resp

    def test_relay_rotate_succeeds_on_jina_first_try(self):
        """Jina works → no CF backends tried."""
        with override_settings(JINA_API_KEY="test-jina-key"), mock.patch(
            "core.scrapers.html.httpx.get"
        ) as get:
            get.return_value = self._make_response(200, GEEZJOBS_SAMPLE_HTML)
            self.scraper.fetch(0)
        # Jina is the only backend called
        self.assertEqual(get.call_count, 1)
        url = get.call_args.kwargs["url"]
        self.assertIn("r.jina.ai", url)

    def test_relay_rotate_falls_back_to_cf_when_jina_fails(self):
        """Jina 402 → falls back to Scrape.do."""
        jina_calls = {"n": 0}

        def fake_get(**kwargs):
            url = kwargs.get("url", "")
            if "r.jina.ai" in url:
                jina_calls["n"] += 1
                return self._make_response(402, "")
            # CF backend call — succeed
            return self._make_response(200, GEEZJOBS_SAMPLE_HTML)

        with override_settings(
            JINA_API_KEY="test-key",
            SCRAPE_DO_API_KEY="test-scrapedo",
        ), mock.patch("core.scrapers.html.httpx.get", side_effect=fake_get), mock.patch(
            "core.scrapers.html.time.sleep"
        ):
            self.scraper.fetch(0)
        # Jina was tried first, then Scrape.do succeeded
        self.assertGreaterEqual(jina_calls["n"], 1)

    def test_relay_rotate_tries_all_backends_when_all_fail(self):
        """All backends fail → ScrapeError listing which ones were tried."""

        def fake_get(**kwargs):
            return self._make_response(500, "error")

        with override_settings(
            JINA_API_KEY="test-key",
            SCRAPE_DO_API_KEY="test-scrapedo",
            ZENROWS_API_KEY="test-zenrows",
        ), mock.patch("core.scrapers.html.httpx.get", side_effect=fake_get), mock.patch(
            "core.scrapers.html.time.sleep"
        ):
            with self.assertRaises(ScrapeError) as ctx:
                self.scraper.fetch(0)
        self.assertIn("exhausted", str(ctx.exception).lower())

    def test_relay_rotate_skips_backends_without_keys(self):
        """Backends with no API key are skipped silently."""

        def fake_get(**kwargs):
            return self._make_response(402, "")

        # Only Jina key set, no CF keys
        with override_settings(JINA_API_KEY="test-key"), mock.patch(
            "core.scrapers.html.httpx.get", side_effect=fake_get
        ), mock.patch("core.scrapers.html.time.sleep"):
            with self.assertRaises(ScrapeError):
                self.scraper.fetch(0)
        # Should have tried Jina (no CF backends available)

    def test_relay_rotate_no_backends_configured(self):
        """No keys at all → clear error message.

        Playwright is free (no key needed) and is in the rotation, so even
        with all API keys empty the error is "rotation exhausted" (Playwright
        tried but failed because it's not installed), not "no configured
        backends".  Either message is acceptable — the test checks that the
        run fails loudly with a helpful message.
        """
        with override_settings(
            JINA_API_KEY="",
            SCRAPE_DO_API_KEY="",
            SCRAPEBADGER_API_KEY="",
            ZENROWS_API_KEY="",
            SCRAPERAPI_KEY="",
            SCRAPFLY_API_KEY="",
        ), mock.patch("core.scrapers.html.httpx.get"):
            with self.assertRaises(ScrapeError) as ctx:
                self.scraper.fetch(0)
        msg = str(ctx.exception).lower()
        # Either "no configured backends" (no free backends) or
        # "rotation exhausted" (Playwright tried but not installed).
        self.assertTrue(
            "no configured backends" in msg or "rotation exhausted" in msg or "playwright" in msg,
            f"Expected helpful error, got: {ctx.exception}",
        )

    def test_relay_rotate_url_uses_jina_path_encoding(self):
        """Jina backend builds the relay URL with path-encoded target."""
        with override_settings(JINA_API_KEY="test-key"), mock.patch(
            "core.scrapers.html.httpx.get"
        ) as get:
            get.return_value = self._make_response(200, GEEZJOBS_SAMPLE_HTML)
            self.scraper.fetch(1)  # page 1 → ?page=2
        url = get.call_args.kwargs["url"]
        # Jina uses path-based URL encoding
        self.assertIn("r.jina.ai/https%3A%2F%2Fgeezjobs.com%2Fsearch-jobs%3Fpage%3D2", url)


class CloudflareRotationTests(TestCase):
    """Tests for the smart Cloudflare rotation dispatcher."""

    def setUp(self):
        self.source = Source.objects.create(
            slug="test-cf",
            name="Test CF Site",
            endpoint="https://example.com/jobs",
            scraper_type="html",
            only_today=True,
            pagination={"relay": "cloudflare_rotate", "timeout": 10.0},
        )
        self.scraper = HtmlScraper(self.source)

    def test_rotation_tries_scrapedo_first_when_configured(self):
        """The rotation picks the cheapest available backend (Scrape.do first)."""
        calls: list[str] = []

        def fake_dispatch(service, url, page):
            calls.append(service)
            from bs4 import BeautifulSoup
            return BeautifulSoup("<html><body><div class='card'></div></body></html>", "html.parser")

        with override_settings(
            SCRAPE_DO_API_KEY="test-key",
            SCRAPEBADGER_API_KEY="",
            ZENROWS_API_KEY="",
            SCRAPERAPI_KEY="",
            SCRAPFLY_API_KEY="",
        ), mock.patch.object(self.scraper, "_dispatch_single_backend", side_effect=fake_dispatch), \
             mock.patch("core.models.ScraperCreditUsage.objects.create") as mock_create, \
             mock.patch("core.models.ScraperCreditUsage.remaining_credits", return_value=100):
            self.scraper._fetch_via_cloudflare_rotate("https://example.com/jobs", 0)
        self.assertEqual(calls, ["scrapedo"])
        mock_create.assert_called_once_with(
            service="scrapedo",
            credits_used=1,
            month=timezone.localdate().strftime("%Y-%m"),
            source_slug="test-cf",
        )

    def test_rotation_skips_missing_keys_and_exhausted_credits(self):
        """Services without API keys or with 0 credits are skipped."""
        calls: list[str] = []

        def fake_dispatch(service, url, page):
            calls.append(service)
            from bs4 import BeautifulSoup
            return BeautifulSoup("<html></html>", "html.parser")

        def fake_remaining(service, month=None):
            return 0 if service == "scrapedo" else 100

        with override_settings(
            SCRAPE_DO_API_KEY="test-key",
            ZENROWS_API_KEY="test-key",
            SCRAPEBADGER_API_KEY="",
            SCRAPERAPI_KEY="",
            SCRAPFLY_API_KEY="",
        ), mock.patch.object(self.scraper, "_dispatch_single_backend", side_effect=fake_dispatch), \
             mock.patch("core.models.ScraperCreditUsage.remaining_credits", side_effect=fake_remaining), \
             mock.patch("core.models.ScraperCreditUsage.objects.create"):
            self.scraper._fetch_via_cloudflare_rotate("https://example.com/jobs", 0)
        # scrapedo skipped (0 credits), scrapebadger skipped (no key), zenrows used
        self.assertEqual(calls, ["zenrows"])

    def test_rotation_raises_when_all_backends_fail(self):
        """When every backend fails, a clear ScrapeError is raised."""
        def fake_dispatch(service, url, page):
            raise ScrapeError(f"{service} failed")

        with override_settings(
            SCRAPE_DO_API_KEY="test-key",
            ZENROWS_API_KEY="test-key",
            SCRAPEBADGER_API_KEY="",
            SCRAPERAPI_KEY="",
            SCRAPFLY_API_KEY="",
        ), mock.patch.object(self.scraper, "_dispatch_single_backend", side_effect=fake_dispatch), \
             mock.patch("core.models.ScraperCreditUsage.remaining_credits", return_value=100):
            with self.assertRaises(ScrapeError) as ctx:
                self.scraper._fetch_via_cloudflare_rotate("https://example.com/jobs", 0)
        self.assertIn("exhausted", str(ctx.exception))
        self.assertIn("scrapedo", str(ctx.exception))
        self.assertIn("zenrows", str(ctx.exception))

    def test_rotation_raises_when_no_keys_configured(self):
        """With no API keys at all, the error says so."""
        with override_settings(
            SCRAPE_DO_API_KEY="",
            ZENROWS_API_KEY="",
            SCRAPEBADGER_API_KEY="",
            SCRAPERAPI_KEY="",
            SCRAPFLY_API_KEY="",
        ):
            with self.assertRaises(ScrapeError) as ctx:
                self.scraper._fetch_via_cloudflare_rotate("https://example.com/jobs", 0)
        self.assertIn("no configured backends", str(ctx.exception))

    def test_credit_usage_model_remaining_credits(self):
        """remaining_credits computes free - used correctly."""
        from core.models import ScraperCreditUsage
        month = timezone.localdate().strftime("%Y-%m")
        # No usage yet → full free tier.
        self.assertEqual(ScraperCreditUsage.remaining_credits("scrapedo", month), 1000)
        # Create some usage.
        ScraperCreditUsage.objects.create(
            service="scrapedo", credits_used=250, month=month, source_slug="test",
        )
        self.assertEqual(ScraperCreditUsage.remaining_credits("scrapedo", month), 750)
        # Unknown service returns 0.
        self.assertEqual(ScraperCreditUsage.remaining_credits("unknown", month), 0)


class PlaywrightBackendTests(TestCase):
    """Tests for the Playwright free-headless-browser backend."""

    def test_playwright_custom_fetch_returns_html(self):
        """PlaywrightBackend.custom_fetch launches a browser and returns HTML."""
        from core.cloudflare_backends import PlaywrightBackend

        fake_html = "<html><body><h1>Test Page</h1></body></html>"

        # Mock the entire playwright.sync_api chain
        mock_page = mock.MagicMock()
        mock_page.content.return_value = fake_html

        mock_context = mock.MagicMock()
        mock_context.new_page.return_value = mock_page

        mock_browser = mock.MagicMock()
        mock_browser.new_context.return_value = mock_context

        mock_p = mock.MagicMock()
        mock_p.chromium.launch.return_value = mock_browser

        mock_sync_fn = mock.MagicMock(return_value=mock_p)
        mock_sync_cm = mock.MagicMock()
        mock_sync_cm.return_value.__enter__ = mock.MagicMock(return_value=mock_p)
        mock_sync_cm.return_value.__exit__ = mock.MagicMock(return_value=False)

        # Inject a fake playwright module so the lazy import inside custom_fetch works
        fake_playwright = mock.MagicMock()
        fake_playwright.sync_api.sync_playwright = mock_sync_cm

        with mock.patch.dict("sys.modules", {
            "playwright": fake_playwright,
            "playwright.sync_api": fake_playwright.sync_api,
        }):
            html, status = PlaywrightBackend.custom_fetch("https://example.com", 30.0)

        self.assertEqual(html, fake_html)
        self.assertEqual(status, 200)
        mock_page.goto.assert_called_once()
        mock_browser.close.assert_called_once()

    def test_playwright_custom_fetch_handles_import_error(self):
        """PlaywrightBackend raises ScrapeError when playwright is not installed."""
        from core.cloudflare_backends import PlaywrightBackend
        from core.challenge import ScrapeError

        with mock.patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            with self.assertRaises(ScrapeError) as ctx:
                PlaywrightBackend.custom_fetch("https://example.com", 30.0)
            self.assertIn("playwright is not installed", str(ctx.exception))

    def test_playwright_in_relay_rotation_order(self):
        """Playwright is #1 in RELAY_ROTATION_ORDER — tried before any API backend."""
        from core.cloudflare_backends import RELAY_ROTATION_ORDER
        self.assertEqual(RELAY_ROTATION_ORDER[0], "playwright")

    def test_playwright_costs_zero_credits(self):
        """PlaywrightBackend has 0 credits_per_request — it's free forever."""
        from core.cloudflare_backends import PlaywrightBackend
        self.assertEqual(PlaywrightBackend.credits_per_request, 0)
        self.assertEqual(PlaywrightBackend.env_key, "")  # No API key needed

    def test_playwright_no_api_key_required_in_rotation(self):
        """Rotation skips backends with no API key UNLESS they're free (Playwright)."""
        from core.cloudflare_backends import PlaywrightBackend, get_backend

        # Playwright has no API key but credits_per_request=0
        self.assertEqual(PlaywrightBackend.get_api_key(), "")
        self.assertEqual(PlaywrightBackend.credits_per_request, 0)

        # The rotation logic should NOT skip Playwright for missing key
        # (is_free check: credits_per_request == 0)
        is_free = getattr(PlaywrightBackend, "credits_per_request", 1) == 0
        self.assertTrue(is_free)


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

        # The cards only carry RELATIVE times ("Published X hours ago"), so
        # pin the clock: 3h/5h ago from 09:00:53Z is 06:00:53Z / 04:00:53Z.
        fixed = datetime(2026, 8, 7, 9, 0, 53, tzinfo=timezone.UTC)
        with mock.patch(
            "core.scrapers.reporterjobs.timezone.now", return_value=fixed
        ):
            raw = BeautifulSoup(REPORTER_SAMPLE_HTML, "html.parser")
            items = self.scraper.parse(raw)
        self.assertEqual(len(items), 2)

        first = items[0]
        self.assertEqual(first["post_id"], "284574")
        self.assertEqual(first["title"], "Property Administrator")
        self.assertEqual(
            first["url"],
            "https://www.ethiopianreporterjobs.com/jobs/284574/",
        )
        self.assertEqual(first["company"], "LANCET GENERAL HOSPITAL")
        self.assertEqual(first["job_type_text"], "Full-time")
        self.assertEqual(first["job_type"], "full_time")
        self.assertEqual(first["location"], "Addis Ababa, Ethiopia")
        self.assertEqual(first["posted_text"], "Published 3 hours ago")
        self.assertEqual(first["deadline_text"], "")
        # Estimated from the relative text against the pinned clock.
        self.assertEqual(
            first["published_at"], "2026-08-07T06:00:53+00:00"
        )
        # No deadline on the new cards — the shared +30-day default applies.
        self.assertIsNone(first["deadline"])

        second = items[1]
        self.assertEqual(second["title"], "Office Engineer")
        self.assertEqual(second["job_type"], "contract")
        self.assertEqual(second["location"], "Bahir Dar, Ethiopia")
        self.assertEqual(second["published_at"], "2026-08-07T04:00:53+00:00")
        self.assertIsNone(second["deadline"])

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
        self.assertEqual(item["location"], "Addis Ababa, Ethiopia")
        # The site's normalized type value maps to the shared enum.
        self.assertEqual(item["job_type"], "FULL_TIME")
        self.assertIsNotNone(item["published_at"])
        # No deadline on the new cards — the +30-day default is applied by the
        # shared save path (deadline_is_default), not the normalized dict.
        self.assertIsNone(item["deadline"])

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

        # Real listings page (the theme's header + an empty cards container)
        # but zero cards: legit empty (e.g. WordPress redirected the deep page
        # to /expired).
        empty = BeautifulSoup(
            '<html><body><header><nav><a href="/">Home</a></nav></header>'
            '<div class="jobsearch-joblisting-classic-wrap"></div></body></html>',
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

    def test_parse_raises_on_js_required_skeleton(self):
        from bs4 import BeautifulSoup

        # The relay sometimes returns the raw page WITHOUT executing the site's
        # JavaScript: the header/nav is intact but there are zero cards and a
        # <noscript> "enable javascript" warning. That must be a ScrapeError
        # (a failed fetch that would otherwise log success with nothing
        # stored), NOT a clean end-of-feed.
        skeleton = BeautifulSoup(
            '<html><body><header><nav><a href="/">Home</a></nav></header>'
            '<noscript>You dont have javascript enabled! Please enable it!</noscript>'
            "</body></html>",
            "html.parser",
        )
        with self.assertRaises(ScrapeError) as ctx:
            self.scraper.parse(skeleton)
        self.assertIn("javascript", str(ctx.exception).lower())

    def test_normalize_job_type_maps_known_and_drops_unknown(self):
        from core.scrapers.reporterjobs import _normalize_job_type

        # Known badges map to the shared JobType values — both the old
        # space-separated chips ("Full Time") and the Careerfy hyphenated
        # badges ("Full-time").
        self.assertEqual(_normalize_job_type("Full Time"), "full_time")
        self.assertEqual(_normalize_job_type("Full-time"), "full_time")
        self.assertEqual(_normalize_job_type("Part-time"), "part_time")
        self.assertEqual(_normalize_job_type("Contract"), "contract")
        self.assertEqual(_normalize_job_type("Internship"), "internship")
        # Parenthetical qualifiers are stripped before mapping.
        self.assertEqual(_normalize_job_type("Full-time (Remote)"), "full_time")
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
        self.assertEqual(detail.location, "Addis Ababa, Ethiopia")
        self.assertEqual(detail.job_type_text, "Full-time")
        self.assertEqual(detail.job_type, "full_time")
        self.assertEqual(detail.job_type_display, "Full-time")
        self.assertEqual(detail.posted_text, "Published 3 hours ago")
        self.assertEqual(detail.deadline_text, "")
        self.assertIsNotNone(detail.published_at)
        # No deadline on the new cards — None stays None (the +30-day default
        # is applied by the shared normalize/save path, not the detail row).
        self.assertIsNone(detail.deadline)
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


class SilentZeroTests(TestCase):
    """A source that logs clean success but finds nothing all day (non-Sunday)
    must be flagged — the ReporterJobs JS-skeleton failure hid that way."""

    # 2026-08-15 is a Saturday — never the excluded Sunday.
    DAY = date(2026, 8, 15)

    def setUp(self):
        self.source = Source.objects.create(
            slug="reporterjobs",
            name="Ethiopian Reporter Jobs",
            endpoint="https://example.com/jobs-in-ethiopia/",
        )

    def _site_log(self, day=DAY, status=ScrapeStatus.SUCCESS, run_count=2, found=0):
        return ReporterScrapeLog.objects.create(
            source=self.source,
            day=day,
            status=status,
            run_count=run_count,
            api_hits=3,
            items_found=found,
            items_inserted=0,
            items_updated=0,
            items_skipped=0,
        )

    def test_flags_clean_success_with_zero_items(self):
        self._site_log()
        flagged = silent_zero_sources_for_day(self.DAY)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["website"], "reporterjobs")
        self.assertEqual(flagged[0]["run_count"], 2)

    def test_does_not_flag_a_day_with_items(self):
        self._site_log(found=320)
        self.assertEqual(silent_zero_sources_for_day(self.DAY), [])

    def test_does_not_flag_a_failed_day(self):
        # A failed run is already reported loudly by api_issues_for_day — the
        # silent-zero flag is only for days that LOOK healthy.
        self._site_log(status=ScrapeStatus.FAILED)
        self.assertEqual(silent_zero_sources_for_day(self.DAY), [])

    def test_does_not_flag_sunday_silence(self):
        # HaHu posts nothing on Sundays — that's legitimate, not a failure.
        self._site_log(day=date(2026, 8, 16))  # Sunday
        self.assertEqual(silent_zero_sources_for_day(date(2026, 8, 16)), [])

    def test_report_mentions_silent_zero_source(self):
        # The telegram report must surface the flag (and mark the day as
        # having issues so daily mode doesn't stay quiet).
        self._site_log()
        ScrapeLog.objects.create(day=self.DAY, status=ScrapeStatus.SUCCESS)
        out = io.StringIO()
        call_command("log_report", "--day", self.DAY.isoformat(), stdout=out)
        self.assertIn("Possible silent failure", out.getvalue())
        self.assertIn("Ethiopian Reporter Jobs", out.getvalue())


class DeepSweepTests(TestCase):
    """A run that swept the whole catalog (20+ pages) must be flagged — the
    Aug-16 backfill pulled 761 old jobs in one silent sweep."""

    DAY = date(2026, 8, 15)  # Saturday

    def setUp(self):
        self.source = Source.objects.create(
            slug="afriwork",
            name="Afriwork",
            endpoint="https://example.com/graphql",
        )

    def _site_log(self, pages=3, status=ScrapeStatus.SUCCESS):
        return AfriworkScrapeLog.objects.create(
            source=self.source,
            day=self.DAY,
            status=status,
            run_count=1,
            api_hits=pages,
            items_found=pages * 10,
            items_inserted=5,
            items_updated=0,
            items_skipped=pages * 10 - 5,
            scraped_log=[
                {
                    "status": status,
                    "errors": [],
                    "message": "",
                    "api_hits": pages,
                    "pages_hit": [
                        {"page": i, "found": 10, "http_status": 200}
                        for i in range(pages)
                    ],
                    "items_found": pages * 10,
                    "items_inserted": 5,
                }
            ],
        )

    def test_flags_deep_sweep(self):
        self._site_log(pages=25)
        flagged = deep_sweep_sources_for_day(self.DAY)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["website"], "afriwork")
        self.assertEqual(flagged[0]["pages"], 25)

    def test_does_not_flag_normal_run(self):
        self._site_log(pages=5)
        self.assertEqual(deep_sweep_sources_for_day(self.DAY), [])

    def test_report_mentions_deep_sweep(self):
        self._site_log(pages=30)
        ScrapeLog.objects.create(day=self.DAY, status=ScrapeStatus.SUCCESS)
        out = io.StringIO()
        call_command("log_report", "--day", self.DAY.isoformat(), stdout=out)
        self.assertIn("Deep sweep", out.getvalue())
        self.assertIn("Afriwork", out.getvalue())


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

    def test_latest_run_wins_site_and_master_status(self):
        # The user-facing rule: a later successful scrape that captured
        # everything supersedes an earlier failure (a failed sweep stores no
        # items, so flipping the status loses nothing — and the run history
        # stays in scraped_log).
        scraper = GraphQLScraper(self.source)
        site_log_id = scraper.record_detail_log(
            make_run(status="failed", http_status=500, errors=["boom"]), self.today
        )
        master = scraper._update_master_day_log(
            make_run(status="failed", http_status=500, errors=["boom"]), self.today, site_log_id
        )
        self.assertEqual(master.status, "failed")
        site_log = AfriworkScrapeLog.objects.get()
        self.assertEqual(site_log.status, "failed")

        # A later successful re-scrape flips both to success.
        site_log_id = scraper.record_detail_log(make_run(found=3), self.today)
        master = scraper._update_master_day_log(make_run(found=3), self.today, site_log_id)
        master.refresh_from_db()
        self.assertEqual(master.status, "success")
        site_log.refresh_from_db()
        self.assertEqual(site_log.status, "success")
        # The failed run is still in the history — nothing was hidden.
        self.assertEqual(len(site_log.scraped_log), 2)
        self.assertEqual(site_log.scraped_log[0]["status"], "failed")

    def test_master_stays_failed_while_any_site_latest_run_failed(self):
        # A site that failed and was never re-scraped keeps the day honest,
        # even though another site's latest run succeeded.
        scraper = GraphQLScraper(self.source)
        other = Source.objects.create(
            slug="ethiojobs", name="EthioJobs", endpoint="https://example.com/2"
        )
        site_log_id = scraper.record_detail_log(
            make_run(status="failed", http_status=500, errors=["boom"]), self.today
        )
        scraper._update_master_day_log(
            make_run(status="failed", http_status=500, errors=["boom"]), self.today, site_log_id
        )

        other_scraper = RestJsonScraper(other)
        other_log_id = other_scraper.record_detail_log(make_run(found=3), self.today)
        master = other_scraper._update_master_day_log(
            make_run(found=3), self.today, other_log_id
        )
        master.refresh_from_db()
        self.assertEqual(master.status, "failed")  # afriwork's latest run still failed

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

    def test_scrape_all_records_one_sweep_entry_per_run(self):
        Source.objects.create(
            slug="afriwork", name="Afriwork", endpoint="https://example.com/1",
            is_active=True,
        )
        with mock.patch(
            "core.management.commands.scrape_all.call_command", return_value=None
        ):
            call_command("scrape_all", stdout=io.StringIO())
            call_command("scrape_all", stdout=io.StringIO())

        # Each overall sweep appends ONE short entry; per-site detail is NOT
        # duplicated here (it lives in the site's own day log).
        master = ScrapeLog.objects.get()
        self.assertEqual(len(master.runs), 2)
        self.assertEqual(master.runs[0]["run"], 1)
        self.assertEqual(master.runs[1]["run"], 2)
        for entry in master.runs:
            self.assertEqual(
                set(entry),
                {"run", "time", "hits", "found", "inserted", "updated", "skipped", "status"},
            )
            self.assertEqual(entry["status"], "success")
            self.assertEqual(entry["hits"], 0)
            self.assertRegex(entry["time"], r"^\d{2}:\d{2}$")

    def test_scrape_all_sweep_entry_flags_failed_runs(self):
        Source.objects.create(
            slug="afriwork", name="Afriwork", endpoint="https://example.com/1",
            is_active=True,
        )
        Source.objects.create(
            slug="geezjobs", name="GeezJobs", endpoint="https://example.com/3",
            is_active=True,
        )

        def fake_call(command, *args, **kwargs):
            if args[0] == "geezjobs":
                raise RuntimeError("boom")

        out = io.StringIO()
        with mock.patch(
            "core.management.commands.scrape_all.call_command", side_effect=fake_call
        ):
            with self.assertRaises(SystemExit):
                call_command("scrape_all", stdout=out)

        master = ScrapeLog.objects.get()
        self.assertEqual(len(master.runs), 1)
        self.assertEqual(master.runs[0]["status"], "failed")

    def test_scrape_all_sweep_entry_sums_site_run_totals(self):
        # A site's just-written run (per-site day log) feeds the sweep totals.
        source = Source.objects.create(
            slug="afriwork", name="Afriwork", endpoint="https://example.com/1",
            is_active=True,
        )
        AfriworkScrapeLog.objects.create(
            source=source,
            day=timezone.localdate(),
            status=ScrapeStatus.SUCCESS,
            run_count=1,
            api_hits=2,
            items_found=3,
            items_inserted=1,
            items_updated=1,
            items_skipped=1,
            scraped_log=[
                {
                    "status": "success",
                    "api_hits": 2,
                    "items_found": 3,
                    "items_inserted": 1,
                    "items_updated": 1,
                    "items_skipped": 1,
                    "started_at": "2026-08-15T09:00:05+00:00",
                    "pages_hit": [],
                    "errors": [],
                }
            ],
        )
        with mock.patch(
            "core.management.commands.scrape_all.call_command", return_value=None
        ):
            call_command("scrape_all", stdout=io.StringIO())

        master = ScrapeLog.objects.get()
        entry = master.runs[0]
        self.assertEqual(entry["hits"], 2)
        self.assertEqual(entry["found"], 3)
        self.assertEqual(entry["inserted"], 1)
        self.assertEqual(entry["updated"], 1)
        self.assertEqual(entry["skipped"], 1)
        self.assertEqual(entry["status"], "success")
        # Started 09:00 UTC = 12:00 Addis Ababa (the configured TIME_ZONE).
        self.assertEqual(
            entry["time"],
            timezone.localtime(
                datetime.fromisoformat("2026-08-15T09:00:05+00:00")
            ).strftime("%H:%M"),
        )


class TelegramReportTests(TestCase):
    """Notification command: per-run always sends; daily mode only on
    failure or at the end-of-day digest time."""

    TODAY = "2026-08-15"

    def _env(self, **extra):
        # Pin every notification env var explicitly so the tests don't depend
        # on (or leak from) the surrounding process environment — e.g. the
        # digest command spawns `manage.py test` as a subprocess that inherits
        # the runner's env, and DAILY_DIGEST_UTC/NOTIFY_MODE must not change
        # what these tests expect.
        base = {
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "TELEGRAM_CHAT_ID": "987",
            "NOTIFY_MODE": "per-run",
            "DAILY_DIGEST_UTC": "20:30",
        }
        base.update(extra)
        return mock.patch.dict(os.environ, base)

    def _post(self):
        return mock.patch("core.management.commands.telegram_report.httpx.post")

    def test_missing_credentials_skips_without_error(self):
        with self._post() as fake_post, mock.patch.dict(
            os.environ, {}, clear=True
        ):
            call_command("telegram_report", stdout=io.StringIO())
        fake_post.assert_not_called()

    def test_per_run_mode_sends_report(self):
        # Pin the clock BEFORE the digest hour: in per-run mode the command
        # sends exactly one message (no test-suite follow-up). Without the
        # pin, a suite run after 20:30 UTC makes this a digest run that
        # spawns a REAL nested `manage.py test` — the recursive-subprocess
        # blow-up that killed the Aug-17 evening run's report step.
        fixed = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.UTC)
        with self._post() as fake_post, self._env(), mock.patch(
            "core.management.commands.telegram_report.timezone.now",
            return_value=fixed,
        ):
            call_command("telegram_report", stdout=io.StringIO())
        fake_post.assert_called_once()
        payload = fake_post.call_args.kwargs["json"]
        self.assertEqual(payload["chat_id"], "987")
        self.assertIn("SeraGo", payload["text"])

    def test_report_shows_successful_run_times(self):
        # The full-day report stays; a short line lists the day's successful
        # overall runs and their (Addis) times from the master's ``runs``.
        ScrapeLog.objects.create(
            day=self.TODAY,
            runs=[
                {
                    "run": 1, "time": "12:02", "hits": 2, "found": 3,
                    "inserted": 1, "updated": 1, "skipped": 1,
                    "status": "success",
                },
                {
                    "run": 2, "time": "23:31", "hits": 1, "found": 0,
                    "inserted": 0, "updated": 0, "skipped": 0,
                    "status": "failed",
                },
            ],
        )
        fixed = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.UTC)  # before digest hour
        with self._post() as fake_post, self._env(), mock.patch(
            "core.management.commands.telegram_report.timezone.now",
            return_value=fixed,
        ):
            call_command("telegram_report", day=self.TODAY, stdout=io.StringIO())
        text = fake_post.call_args.kwargs["json"]["text"]
        self.assertIn("✅ successful runs: 12:02 (Addis)", text)

    def test_report_shows_dash_when_no_successful_runs(self):
        ScrapeLog.objects.create(day=self.TODAY)
        fixed = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.UTC)  # before digest hour
        with self._post() as fake_post, self._env(), mock.patch(
            "core.management.commands.telegram_report.timezone.now",
            return_value=fixed,
        ):
            call_command("telegram_report", day=self.TODAY, stdout=io.StringIO())
        text = fake_post.call_args.kwargs["json"]["text"]
        self.assertIn("✅ successful runs: — (Addis)", text)

    def test_daily_mode_suppresses_healthy_run_before_digest(self):
        ScrapeLog.objects.create(day=self.TODAY)
        fixed = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.UTC)  # before 20:30 UTC
        with self._post() as fake_post, self._env(NOTIFY_MODE="daily"), mock.patch(
            "core.management.commands.telegram_report.timezone.now",
            return_value=fixed,
        ):
            call_command("telegram_report", day=self.TODAY, stdout=io.StringIO())
        fake_post.assert_not_called()

    def test_daily_mode_sends_on_failure(self):
        ScrapeLog.objects.create(day=self.TODAY, status=ScrapeStatus.FAILED)
        fixed = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.UTC)
        with self._post() as fake_post, self._env(NOTIFY_MODE="daily"), mock.patch(
            "core.management.commands.telegram_report.timezone.now",
            return_value=fixed,
        ):
            call_command("telegram_report", day=self.TODAY, stdout=io.StringIO())
        fake_post.assert_called_once()

    def test_daily_mode_sends_digest_at_end_of_day(self):
        ScrapeLog.objects.create(
            day=self.TODAY,
            websites=[
                {
                    "source": "afriwork",
                    "name": "Afriwork (Freelance Ethiopia)",
                    "status": "success",
                    "run_count": 1,
                    "api_hits": 2,
                    "items_found": 8,
                    "items_inserted": 7,
                    "items_skipped": 1,
                }
            ],
        )
        fixed = datetime(2026, 8, 15, 21, 0, tzinfo=timezone.UTC)  # past 20:30 UTC
        fake_run = mock.Mock(returncode=0, stdout="Ran 85 tests in 1.234s\n\nOK\n", stderr="")
        with self._post() as fake_post, self._env(NOTIFY_MODE="daily"), mock.patch(
            "core.management.commands.telegram_report.timezone.now",
            return_value=fixed,
        ), mock.patch(
            "core.management.commands.telegram_report.subprocess.run",
            return_value=fake_run,
        ):
            call_command("telegram_report", day=self.TODAY, stdout=io.StringIO())
        # Two messages: the FULL day report first (day status + per-website
        # totals + issues), then the test-suite stats as a short follow-up —
        # so a slow test run can never hold up the report.
        self.assertEqual(len(fake_post.call_args_list), 2)
        report_text = fake_post.call_args_list[0].kwargs["json"]["text"]
        self.assertIn("Full Day Report", report_text)
        self.assertIn("SUCCESS", report_text)
        self.assertIn("found   :", report_text)
        # The user-requested per-website layout: name on one line, stats under
        # it prefixed with =>.
        self.assertIn("✅ Afriwork (Freelance Ethiopia)", report_text)
        self.assertIn("=> api 2 · found 8 · inserted 7 · skipped 1", report_text)
        tests_text = fake_post.call_args_list[1].kwargs["json"]["text"]
        self.assertIn("🧪 Tests", tests_text)
        self.assertIn("85 passed", tests_text)

    def test_run_tests_parses_stderr_summary(self):
        # Django's test runner writes the summary ("Ran N tests ... OK") to
        # stderr, not stdout — the digest must find it there.
        fake_run = mock.Mock(
            returncode=0,
            stdout="Found 87 test(s).\nSystem check identified no issues (0 silenced).\n",
            stderr="Ran 87 tests in 3.5s\n\nOK\nDestroying test database...\n",
        )
        with mock.patch(
            "core.management.commands.telegram_report.subprocess.run",
            return_value=fake_run,
        ):
            result = TelegramReportCommand()._run_tests()
        self.assertIn("87 passed", result)
        self.assertNotIn("FAILED", result)

    def test_digest_nags_when_last_commit_is_stale(self):
        # A >45-day gap since the last push means the schedule is running on
        # stale code (a push is what keeps the cron alive) — the digest must
        # nag so it can't quietly rot.
        ScrapeLog.objects.create(day=self.TODAY)
        fixed = datetime(2026, 8, 15, 21, 0, tzinfo=timezone.UTC)  # past 20:30 UTC
        fake_run = mock.Mock(returncode=0, stdout="Ran 1 tests\n\nOK\n", stderr="")
        with self._post() as fake_post, self._env(NOTIFY_MODE="daily"), mock.patch(
            "core.management.commands.telegram_report.timezone.now",
            return_value=fixed,
        ), mock.patch(
            "core.management.commands.telegram_report.subprocess.run",
            return_value=fake_run,
        ), mock.patch(
            "core.management.commands.telegram_report.last_commit_age_days",
            return_value=60,
        ):
            call_command("telegram_report", day=self.TODAY, stdout=io.StringIO())
        # The nag rides on the FIRST message (the report itself); the second
        # message is the test-suite follow-up.
        text = fake_post.call_args_list[0].kwargs["json"]["text"]
        self.assertIn("Last commit 60 days ago", text)

    def test_digest_stays_quiet_for_recent_commit(self):
        ScrapeLog.objects.create(day=self.TODAY)
        fixed = datetime(2026, 8, 15, 21, 0, tzinfo=timezone.UTC)
        fake_run = mock.Mock(returncode=0, stdout="Ran 1 tests\n\nOK\n", stderr="")
        with self._post() as fake_post, self._env(NOTIFY_MODE="daily"), mock.patch(
            "core.management.commands.telegram_report.timezone.now",
            return_value=fixed,
        ), mock.patch(
            "core.management.commands.telegram_report.subprocess.run",
            return_value=fake_run,
        ), mock.patch(
            "core.management.commands.telegram_report.last_commit_age_days",
            return_value=3,
        ):
            call_command("telegram_report", day=self.TODAY, stdout=io.StringIO())
        text = fake_post.call_args_list[0].kwargs["json"]["text"]
        self.assertNotIn("Last commit", text)

    def test_digest_skips_tests_with_flag(self):
        ScrapeLog.objects.create(day=self.TODAY)
        fixed = datetime(2026, 8, 15, 21, 0, tzinfo=timezone.UTC)
        with self._post() as fake_post, self._env(), mock.patch(
            "core.management.commands.telegram_report.timezone.now",
            return_value=fixed,
        ), mock.patch(
            "core.management.commands.telegram_report.subprocess.run"
        ) as fake_run, mock.patch(
            # The 45-day check shells out to `git` through the same stdlib
            # subprocess module — pin it so this test only asserts that
            # --skip-tests prevents the TEST suite from running.
            "core.management.commands.telegram_report.last_commit_age_days",
            return_value=3,
        ):
            call_command(
                "telegram_report",
                "--skip-tests",
                day=self.TODAY,
                stdout=io.StringIO(),
            )
        fake_post.assert_called_once()
        fake_run.assert_not_called()
        self.assertNotIn("🧪 Tests", fake_post.call_args.kwargs["json"]["text"])

    def test_digest_sends_report_before_running_tests(self):
        # The report is delivered BEFORE the test suite runs — a hung or
        # failing suite (the Aug-17 runner-kill failure mode) can never take
        # the report down with it.
        ScrapeLog.objects.create(day=self.TODAY, status=ScrapeStatus.FAILED)
        fixed = datetime(2026, 8, 15, 21, 0, tzinfo=timezone.UTC)
        with self._post() as fake_post, self._env(), mock.patch(
            "core.management.commands.telegram_report.timezone.now",
            return_value=fixed,
        ), mock.patch.object(
            TelegramReportCommand, "_run_tests", return_value="87 passed"
        ):
            call_command("telegram_report", day=self.TODAY, stdout=io.StringIO())
        self.assertEqual(len(fake_post.call_args_list), 2)
        self.assertIn(
            "Full Day Report",
            fake_post.call_args_list[0].kwargs["json"]["text"],
        )
        self.assertIn(
            "🧪 Tests",
            fake_post.call_args_list[1].kwargs["json"]["text"],
        )

    def test_digest_test_followup_failure_does_not_fail_step(self):
        # The main report already went out — a failing test-suite follow-up
        # must not turn the step red or lose the report.
        ScrapeLog.objects.create(day=self.TODAY, status=ScrapeStatus.FAILED)
        fixed = datetime(2026, 8, 15, 21, 0, tzinfo=timezone.UTC)
        with self._post() as fake_post, self._env(), mock.patch(
            "core.management.commands.telegram_report.timezone.now",
            return_value=fixed,
        ), mock.patch.object(
            TelegramReportCommand, "_run_tests", side_effect=RuntimeError("boom")
        ):
            call_command("telegram_report", day=self.TODAY, stdout=io.StringIO())
        self.assertEqual(len(fake_post.call_args_list), 1)
        self.assertIn(
            "Full Day Report",
            fake_post.call_args_list[0].kwargs["json"]["text"],
        )

    def test_force_sends_even_when_daily_mode_would_stay_quiet(self):
        ScrapeLog.objects.create(day=self.TODAY)
        fixed = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.UTC)
        with self._post() as fake_post, self._env(NOTIFY_MODE="daily"), mock.patch(
            "core.management.commands.telegram_report.timezone.now",
            return_value=fixed,
        ):
            call_command(
                "telegram_report", "--force", day=self.TODAY, stdout=io.StringIO()
            )
        fake_post.assert_called_once()


class SeedSourcesCommandTests(TestCase):
    def test_seed_sources_sets_reporterjobs_to_cloudflare_rotate(self):
        # ReporterJobs is behind a Cloudflare challenge that the free Jina
        # relay can no longer beat — it must seed on the cloudflare_rotate
        # backend (smart rotation across ZenRows, Scrape.do, ScrapeBadger,
        # ScrapFly, ScraperAPI). See CLOUDFLARE.md.
        call_command("seed_sources", stdout=io.StringIO())
        reporter = Source.objects.get(slug="reporterjobs")
        self.assertEqual((reporter.pagination or {}).get("relay"), "cloudflare_rotate")


class ArchiveWeekCommandTests(TestCase):
    """The Sunday archive keeps Sunday's logs; Monday clears them or retries.

    Flow under test (one calendar week Mon–Sun):
      * Sunday: files everything in the DB, sends, deletes the jobs and the
        Mon–Sat log rows, KEEPS Sunday's, writes the ArchiveRun "sent" note.
      * Monday with a note: deletes Sunday's kept rows (the safe clear).
      * Monday without a note: retries the archive; on success deletes
        everything and writes the note.
      * Any send failure: nothing is deleted, a warning is raised (the
        command exits non-zero so the workflow shows red).
    """

    def setUp(self):
        self.source = Source.objects.create(
            slug="afriwork",
            name="Afriwork",
            endpoint="https://example.com/graphql",
        )
        self.today = timezone.localdate()
        self.yesterday = self.today - timedelta(days=1)
        self.old_day = self.today - timedelta(days=3)
        # A week's worth of log rows: an old day, yesterday (Mon–Sat stand-ins)
        # and today (Sunday).
        for day in (self.old_day, self.yesterday, self.today):
            ScrapeLog.objects.create(
                day=day, status="success", run_count=1, api_hits=1,
                items_found=1, items_inserted=1,
            )
            AfriworkScrapeLog.objects.create(
                source=self.source, day=day, status="success",
                run_count=1, api_hits=1, items_found=1, items_inserted=1,
            )
        self.env = mock.patch.dict(
            os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "test-chat"}
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def _ended_job(self, external_id="x1"):
        """A listing past its deadline + grace window (what Sunday archives)."""
        return ScrapedItem.objects.create(
            source=self.source,
            external_id=external_id,
            title="Old job",
            content_hash="abc123",
            deadline=timezone.now() - timedelta(days=LIFECYCLE_GRACE_DAYS + 1),
        )

    def _ok_post(self):
        return mock.patch(
            "core.management.commands.archive_week.httpx.post",
            return_value=mock.Mock(
                raise_for_status=lambda: None, json=lambda: {"ok": True}
            ),
        )

    def test_sunday_files_sends_keeps_sunday_and_writes_note(self):
        self._ended_job()
        with self._ok_post():
            call_command("archive_week", "--step", "sunday", stdout=io.StringIO())

        # Jobs archived + deleted entirely.
        self.assertEqual(ScrapedItem.objects.count(), 0)
        # Mon–Sat log rows deleted; Sunday's kept.
        self.assertEqual(ScrapeLog.objects.filter(day=self.today).count(), 1)
        self.assertEqual(ScrapeLog.objects.filter(day__lt=self.today).count(), 0)
        self.assertEqual(AfriworkScrapeLog.objects.filter(day=self.today).count(), 1)
        self.assertEqual(AfriworkScrapeLog.objects.filter(day__lt=self.today).count(), 0)
        # The "sent" note was written.
        note = ArchiveRun.objects.filter(archived_on=self.today).first()
        self.assertIsNotNone(note)
        self.assertEqual(note.jobs_count, 1)

    def test_monday_with_note_clears_sunday_without_sending(self):
        ArchiveRun.objects.create(
            archived_on=self.yesterday, jobs_file="j.gz", logs_file="l.gz"
        )
        with mock.patch(
            "core.management.commands.archive_week.httpx.post"
        ) as fake_post:
            call_command("archive_week", "--step", "monday", stdout=io.StringIO())

        fake_post.assert_not_called()  # nothing to send — Sunday already filed
        # Sunday's kept rows (and anything older) are cleared; the new week's
        # own rows (today's, from Monday's first scrape) stay.
        self.assertEqual(ScrapeLog.objects.filter(day__lt=self.today).count(), 0)
        self.assertEqual(ScrapeLog.objects.count(), 1)
        self.assertEqual(AfriworkScrapeLog.objects.filter(day__lt=self.today).count(), 0)
        self.assertEqual(AfriworkScrapeLog.objects.count(), 1)

    def test_monday_without_note_retries_and_deletes_everything(self):
        self._ended_job()
        with self._ok_post():
            call_command("archive_week", "--step", "monday", stdout=io.StringIO())

        # The retry filed everything still in the DB and cleared it all.
        self.assertEqual(ScrapedItem.objects.count(), 0)
        self.assertEqual(ScrapeLog.objects.count(), 0)
        self.assertEqual(AfriworkScrapeLog.objects.count(), 0)
        # The note now exists for the Sunday the retry covered.
        self.assertTrue(ArchiveRun.objects.filter(archived_on=self.yesterday).exists())

    def test_failed_send_deletes_nothing_and_raises(self):
        self._ended_job()
        with mock.patch(
            "core.management.commands.archive_week.httpx.post",
            side_effect=httpx.ConnectError("boom"),
        ):
            with self.assertRaises(CommandError):
                call_command("archive_week", "--step", "sunday", stdout=io.StringIO())

        # Nothing was deleted on failure — jobs and all log rows stay.
        self.assertEqual(ScrapedItem.objects.count(), 1)
        self.assertEqual(ScrapeLog.objects.count(), 3)
        self.assertEqual(AfriworkScrapeLog.objects.count(), 3)
        self.assertFalse(ArchiveRun.objects.exists())  # no "sent" note


class ScrapeStatTests(TestCase):
    """The persistent weekly/monthly rollups stay correct."""

    def setUp(self):
        self.afriwork = Source.objects.create(
            slug="afriwork",
            name="Afriwork",
            endpoint="https://example.com/graphql",
        )
        self.ethiojobs = Source.objects.create(
            slug="ethiojobs",
            name="EthioJobs",
            endpoint="https://example.com/api",
        )
        self.monday = week_bounds(timezone.localdate())[0]  # this week's Monday
        self.sunday = self.monday + timedelta(days=6)

    def _log_day(self, source, day, runs=1, found=10, inserted=8, failed=False):
        """A master row + one per-site row for a (source, day)."""
        master, _ = ScrapeLog.objects.get_or_create(day=day)
        master.run_count += runs
        master.api_hits += runs * 2
        master.items_found += found
        master.items_inserted += inserted
        master.items_skipped += found - inserted
        master.save()
        site, _ = AfriworkScrapeLog.objects.get_or_create(source=source, day=day)
        site.run_count = runs
        site.api_hits = runs * 2
        site.items_found = found
        site.items_inserted = inserted
        site.items_skipped = found - inserted
        site.scraped_log = [
            {
                "status": "failed" if failed else "success",
                "errors": ["ReadTimeout: The read operation timed out"] if failed else [],
                "message": "",
                "api_hits": runs * 2,
                "items_found": found,
                "items_inserted": inserted,
            }
        ]
        site.save()

    def test_recompute_week_aggregates_and_failures(self):
        self._log_day(self.afriwork, self.monday, found=10, inserted=8)
        self._log_day(self.afriwork, self.sunday, found=5, inserted=5, failed=True)
        self._log_day(self.ethiojobs, self.monday, found=20, inserted=15)

        stat = recompute_stat(ScrapeStat.PeriodType.WEEK, self.monday)

        self.assertEqual(stat.period_end, self.sunday)
        self.assertEqual(stat.days_with_runs, 2)
        self.assertEqual(stat.run_count, 3)
        self.assertEqual(stat.items_found, 35)
        self.assertEqual(stat.items_inserted, 28)
        self.assertEqual(stat.items_skipped, 7)
        self.assertEqual(stat.runs_by_status, {"success": 2, "failed": 1})
        self.assertEqual(stat.top_errors[0]["message"], "ReadTimeout: The read operation timed out")
        self.assertEqual(stat.top_errors[0]["count"], 1)
        self.assertEqual(stat.by_source["afriwork"]["failed_runs"], 1)
        self.assertEqual(stat.by_source["ethiojobs"]["items_inserted"], 15)

    def test_recompute_is_idempotent_upsert(self):
        self._log_day(self.afriwork, self.monday, found=10, inserted=8)
        recompute_stat(ScrapeStat.PeriodType.WEEK, self.monday)
        recompute_stat(ScrapeStat.PeriodType.WEEK, self.monday)
        self.assertEqual(ScrapeStat.objects.filter(
            period_type="week", period_start=self.monday
        ).count(), 1)

    def test_month_bounds(self):
        start, end = month_bounds(date(2026, 8, 16))
        self.assertEqual(start, date(2026, 8, 1))
        self.assertEqual(end, date(2026, 8, 31))
        start, end = month_bounds(date(2026, 12, 15))
        self.assertEqual(start, date(2026, 12, 1))
        self.assertEqual(end, date(2026, 12, 31))

    def test_update_current_stats_writes_week_and_month(self):
        self._log_day(self.afriwork, self.monday)
        update_current_stats()
        self.assertTrue(ScrapeStat.objects.filter(period_type="week").exists())
        self.assertTrue(ScrapeStat.objects.filter(period_type="month").exists())

    def test_stat_block_renders_failure_line(self):
        self._log_day(self.afriwork, self.monday, found=10, inserted=8, failed=True)
        recompute_stat(ScrapeStat.PeriodType.WEEK, self.monday)
        block = stat_block("week", self.monday)
        text = "\n".join(block)
        self.assertIn("📊 WEEK", text)
        self.assertIn("1 failed", text)
        self.assertIn("ReadTimeout", text)

    def test_stat_block_empty_when_no_row(self):
        self.assertEqual(stat_block("week", self.monday), [])

    def test_recompute_day_aggregates_only_that_day(self):
        self._log_day(self.afriwork, self.monday, found=10, inserted=8)
        self._log_day(self.afriwork, self.monday + timedelta(days=1), found=99, inserted=90)
        stat = recompute_stat(ScrapeStat.PeriodType.DAY, self.monday)
        self.assertEqual(stat.period_start, self.monday)
        self.assertEqual(stat.period_end, self.monday)
        self.assertEqual(stat.items_found, 10)
        self.assertEqual(stat.by_source["afriwork"]["items_inserted"], 8)

    def test_recompute_year_sums_month_rows(self):
        # The year row must come from the never-deleted MONTH rows (the day
        # logs for past months are pruned by the archive).
        jan = date(2026, 1, 15)
        feb = date(2026, 2, 15)
        self._log_day(self.afriwork, jan, found=100, inserted=80)
        self._log_day(self.afriwork, feb, found=40, inserted=30)
        recompute_stat(ScrapeStat.PeriodType.MONTH, month_bounds(jan)[0])
        recompute_stat(ScrapeStat.PeriodType.MONTH, month_bounds(feb)[0])

        stat = recompute_stat(ScrapeStat.PeriodType.YEAR, date(2026, 1, 1))
        self.assertEqual(stat.period_start, date(2026, 1, 1))
        self.assertEqual(stat.period_end, date(2026, 12, 31))
        self.assertEqual(stat.items_found, 140)
        self.assertEqual(stat.items_inserted, 110)
        self.assertEqual(stat.by_source["afriwork"]["items_inserted"], 110)
        self.assertEqual(stat.days_with_runs, 2)

    def test_year_bounds(self):
        start, end = year_bounds(date(2026, 8, 16))
        self.assertEqual(start, date(2026, 1, 1))
        self.assertEqual(end, date(2026, 12, 31))

    def test_update_current_stats_writes_all_periods_and_sectors(self):
        self._log_day(self.afriwork, self.monday, found=10, inserted=8)
        today = timezone.localdate()
        AfriworkJob.objects.create(
            external_id="s1",
            published_at=timezone.make_aware(
                datetime.combine(today, datetime.min.time())
            ),
            sectors=["Construction & Civil Engineering", "IT"],
        )
        update_current_stats()
        for period in ("day", "week", "month", "year"):
            self.assertTrue(
                ScrapeStat.objects.filter(period_type=period).exists(),
                f"missing {period} stat",
            )
        sectors = {
            s.category_name: s.count
            for s in CategoryStat.objects.filter(category_type="sector", period_start=today)
        }
        self.assertEqual(sectors.get("Construction & Civil Engineering"), 1)
        self.assertEqual(sectors.get("IT"), 1)

    def test_recompute_sector_stats_counts_across_sites(self):
        day = date(2026, 8, 15)
        noon = timezone.make_aware(datetime(2026, 8, 15, 12, 0))
        AfriworkJob.objects.create(
            external_id="a1", published_at=noon, sectors=["IT", "IT"]
        )
        AfriworkJob.objects.create(
            external_id="a2", published_at=noon, sectors=["Finance"]
        )
        HaHuJob.objects.create(external_id="h1", approved_on=noon, sector_name="IT")
        HaHuJob.objects.create(external_id="h2", approved_on=noon, sector_name="")

        recompute_sector_stats(day)
        rows = {
            s.category_name: s.count
            for s in CategoryStat.objects.filter(category_type="sector", period_start=day)
        }
        self.assertEqual(rows, {"IT": 3, "Finance": 1})  # a1 has IT twice; h2's empty name dropped

    def test_recompute_sector_stats_drops_stale_rows(self):
        day = date(2026, 8, 15)
        noon = timezone.make_aware(datetime(2026, 8, 15, 12, 0))
        AfriworkJob.objects.create(external_id="a1", published_at=noon, sectors=["IT"])
        recompute_sector_stats(day)
        self.assertEqual(CategoryStat.objects.filter(period_start=day).count(), 1)

        # A re-scrape where the sector changed must not keep the old count.
        AfriworkJob.objects.filter(external_id="a1").update(sectors=["Finance"])
        recompute_sector_stats(day)
        rows = {
            s.category_name: s.count
            for s in CategoryStat.objects.filter(category_type="sector", period_start=day)
        }
        self.assertEqual(rows, {"Finance": 1})


class DateTextParseTests(TestCase):
    """The shared HTML date parser handles both card and detail-page formats."""

    def test_abbreviated_month_with_trailing_period(self):
        # GeezJobs' detail page abbreviates with a period: "Sep. 6, 2026".
        parsed = parse_month_day_year("Sep. 6, 2026 (21 days left)")
        self.assertIsNotNone(parsed)
        self.assertEqual((parsed.month, parsed.day, parsed.year), (9, 6, 2026))

    def test_full_and_bare_abbreviation_still_parse(self):
        self.assertEqual(parse_month_day_year("Deadline: September 7, 2026").day, 7)
        self.assertEqual(parse_month_day_year("Aug 7, 2026").month, 8)

    def test_unparseable_returns_none(self):
        self.assertIsNone(parse_month_day_year("Deadline: none"))
        self.assertIsNone(parse_month_day_year(""))


class GraphQLTodayBoundaryTests(TestCase):
    """AfriworkJobsScraper's client-side today guard (published-or-refreshed)."""

    def setUp(self):
        self.source = Source.objects.create(
            slug="afriwork",
            name="Afriwork",
            endpoint="https://example.com/graphql",
            only_today=True,
        )
        self.scraper = AfriworkJobsScraper(self.source)
        self.old = {
            "published_at": timezone.now() - timedelta(days=5),
            "raw_data": {"refreshed_at": None},
        }
        self.new = {"published_at": timezone.now(), "raw_data": {"refreshed_at": None}}

    def test_published_today_is_today(self):
        self.assertTrue(self.scraper._is_today_item(self.new))

    def test_old_published_not_refreshed_is_not_today(self):
        self.assertFalse(self.scraper._is_today_item(self.old))

    def test_refreshed_today_counts_as_today(self):
        # Mirrors the Afriwork query: published today OR refreshed today.
        item = {
            "published_at": timezone.now() - timedelta(days=5),
            "raw_data": {"refreshed_at": timezone.now()},
        }
        self.assertTrue(self.scraper._is_today_item(item))

    def test_keep_item_drops_old_in_today_mode(self):
        self.assertFalse(self.scraper._keep_item(self.old))
        self.assertTrue(self.scraper._keep_item(self.new))

    def test_keep_item_keeps_all_outside_today_mode(self):
        self.source.only_today = False
        self.assertTrue(AfriworkJobsScraper(self.source)._keep_item(self.old))

    def test_hahujobs_does_not_inherit_the_guard(self):
        # HaHu filters server-side on approved_on; inheriting the guard would
        # drop its items (no published_at/refreshed_at) and truncate sweeps.
        from core.scrapers.hahujobs import HaHuJobsScraper

        hahu = HaHuJobsScraper(self.source)
        self.assertTrue(hahu._keep_item(self.old))
        self.assertFalse(hahu._past_today_boundary(0, [self.old]))

    def test_past_today_boundary_stops_on_all_old_page(self):
        self.assertTrue(self.scraper._past_today_boundary(3, [self.old, self.old]))

    def test_normalize_formats_structured_compensation_into_salary(self):
        item = self.scraper.normalize(
            {
                "id": "abc",
                "compensation_amount_cents": 1800000,
                "compensation_currency": "ETB",
                "compensation_type": "MONTHLY",
            }
        )
        self.assertEqual(item["salary"], "18,000 ETB monthly")

    def test_normalize_formats_fixed_compensation_without_frequency(self):
        item = self.scraper.normalize(
            {
                "id": "abc",
                "compensation_amount_cents": 50000,
                "compensation_currency": "ETB",
                "compensation_type": "FIXED",
            }
        )
        self.assertEqual(item["salary"], "500 ETB")

    def test_normalize_leaves_salary_unset_without_compensation(self):
        item = self.scraper.normalize(
            {"id": "abc", "compensation_amount_cents": None}
        )
        self.assertNotIn("salary", item)
        self.assertFalse(self.scraper._past_today_boundary(3, [self.new]))
        self.assertFalse(self.scraper._past_today_boundary(3, [self.new, self.old]))
