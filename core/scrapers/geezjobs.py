"""GeezJobs HTML scraper — the fourth website (server-side HTML).

GeezJobs (``https://geezjobs.com``) is fundamentally different from the
previous sites: there is **no JSON API** — the listings are rendered
server-side into ``.opportunity-card`` divs on ``/search-jobs`` (paginated
with ``?page=N``, ~15 cards per page, newest first). It reuses the generic
``HtmlScraper`` pipeline and adds the site-specific parts:

* :meth:`parse` — one raw card dict per ``.opportunity-card``: title, detail
  slug, company, location, deadline, employment, experience, the relative
  ``Posted: X ago`` chip, and the company logo. The page ALSO embeds a JSON-LD
  ``ItemList`` of ``JobPosting`` entries, but the cards carry strictly more
  data, so the cards are the single source of truth.

* Anti-bot: every page embeds a honeypot (``.trap-field`` — a fake
  ``/security/report-scraping`` link plus an ``is_bot`` checkbox). The scraper
  only ever sends GETs and never submits forms, so the trap cannot be
  triggered. A page that returns no cards AND none of the search-filter UI is
  treated as a bot-check/error page and raises :class:`ScrapeError` instead of
  silently recording an empty feed.

* Timestamps: cards only say ``Posted: 3 min ago`` / ``1 hours ago``, so
  ``published_at`` is ESTIMATED as now − offset and drives the client-side
  today filter (``_keep_item``/``_past_today_boundary``, inherited from
  ``HtmlScraper``) plus the per-day job numbering.

* Employment: the site splits a job into a TIME (``full-time`` / ``part-time``)
  and a TYPE (``permanent`` / ``contract`` / ``internship`` / ``freelance`` /
  ``volunteer``). Both raw values are stored on ``GeezJob``; the master
  ``ScrapedItem.job_type`` gets the time-based value (``FULL_TIME`` /
  ``PART_TIME`` — the only ones the shared ``JobType`` enum covers).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from bs4 import BeautifulSoup, Tag
from django.utils import timezone

from core.models import GeezJob, GeezScrapeLog, ScrapedItem

from .base import ScrapeError
from .html import HtmlScraper, parse_month_day_year

# Card text parsers (all chips follow the same row shape).
#   "Posted: 3 min ago" | "Posted: 1 hours ago" | "Posted: 2 days ago"
_POSTED_RE = re.compile(r"Posted:\s*(\d+)\s*(min|hour|day|week)s?\s*ago", re.IGNORECASE)
_POSTED_UNIT_SECONDS = {"min": 60, "hour": 3600, "day": 86400, "week": 604800}
#   "3+ Years" | "2/3 Years" | "6 Years" | "3/5+ Years" (live formats)
_EXPERIENCE_RANGE_RE = re.compile(r"(\d+)\s*/\s*(\d+)", re.IGNORECASE)
_EXPERIENCE_PLUS_RE = re.compile(r"(\d+)\s*\+", re.IGNORECASE)
_EXPERIENCE_PLAIN_RE = re.compile(r"(\d+)\s*years?", re.IGNORECASE)
#   "Full-time / Permanent"
_EMPLOYMENT_SPLIT_RE = re.compile(r"\s*/\s*")
# The site's own filter values (data-filter-key="ti" / "ty").
_JOB_TIME_VALUES = {"full_time", "part_time"}


def _parse_deadline(text: str) -> datetime | None:
    """'Deadline: September 7, 2026' -> aware datetime at local midnight.

    Reuses the shared HTML date-text parser (handles full + abbreviated month
    names; the 'Deadline:' prefix is simply skipped by its search).
    """
    return parse_month_day_year(text)


def _parse_posted(text: str) -> datetime | None:
    """'Posted: 3 min ago' -> now − offset (an ESTIMATE — the site has no exact timestamps)."""
    if not text:
        return None
    match = _POSTED_RE.search(text)
    if not match:
        return None
    seconds = int(match.group(1)) * _POSTED_UNIT_SECONDS[match.group(2).lower()]
    return timezone.now() - timedelta(seconds=seconds)


def _parse_experience(text: str) -> tuple[int | None, int | None]:
    """
    '3+ Years' -> (3, None); '2/3 Years' -> (2, 3); '6 Years' -> (6, 6);
    '3/5+ Years' -> (3, 5) (the trailing '+' just means the upper bound is open).
    """
    if not text:
        return None, None
    match = _EXPERIENCE_RANGE_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = _EXPERIENCE_PLUS_RE.search(text)
    if match:
        return int(match.group(1)), None
    match = _EXPERIENCE_PLAIN_RE.search(text)
    if match:
        value = int(match.group(1))
        return value, value
    return None, None


def _parse_employment(text: str) -> tuple[str, str]:
    """'Full-time / Permanent' -> (job_time, job_type) using the site's values."""
    # 'Full-time' -> 'full_time', 'Part-Time' -> 'part_time' (hyphens too).
    parts = [
        p.strip().lower().replace(" ", "_").replace("-", "_")
        for p in _EMPLOYMENT_SPLIT_RE.split(text)
        if p.strip()
    ]
    if not parts:
        return "", ""
    time_part = next((p for p in parts if p in _JOB_TIME_VALUES), "")
    type_part = next((p for p in parts if p not in _JOB_TIME_VALUES), "")
    return time_part, type_part


def _split_location(text: str) -> tuple[str, str]:
    """'Addis Ababa - Ethiopia' -> ('Addis Ababa', 'Ethiopia')."""
    if not text:
        return "", ""
    parts = [p.strip() for p in re.split(r"\s*-\s*", text) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return text, ""


class GeezJobsScraper(HtmlScraper):
    """GeezJobs — server-side HTML listing pages, paginated by ``?page=N``."""

    site_log_model = GeezScrapeLog

    #: Anchor for the search/filter UI — its presence means we got a real
    #: listings page (as opposed to a bot-check / error / login page).
    _UI_ANCHOR_SELECTOR = ".filter-group, form[action='/search-jobs']"

    @staticmethod
    def _chip_text(card: Tag, icon: str) -> str:
        """Text next to an ``i[data-lucide=icon]`` inside a card.

        Every info row on a card is ``<div><i data-lucide="..."></i><span>text
        </span></div>`` (company uses an ``<a>`` instead of the span), so the
        sibling of the icon's parent row holds the value.
        """
        node = card.select_one(f'i[data-lucide="{icon}"]')
        if node is None:
            return ""
        row = node.parent
        if row is None:
            return ""
        sibling = row.select_one("span, a")
        return " ".join(sibling.get_text().split()) if sibling else ""

    def _absolute_url(self, href: str) -> str:
        """Resolve a relative card href against the source's base URL."""
        if href.startswith("http"):
            return href
        base = (self.source.base_url or "https://geezjobs.com").rstrip("/")
        return f"{base}/{href.lstrip('/')}"

    def parse(self, raw: BeautifulSoup) -> list[dict]:
        """Extract one raw card dict per listing card on the page."""
        cards = raw.select(".opportunity-card")
        if not cards and not raw.select(self._UI_ANCHOR_SELECTOR):
            # No cards AND none of the search UI: this smells like a
            # bot-check / error / login page — fail loudly instead of silently
            # recording an empty feed. (The honeypot is never submitted: we
            # only ever GET listing pages.)
            trap = raw.select_one(".trap-field") is not None
            raise ScrapeError(
                "No job cards or search UI found on the page"
                + (" (site honeypot present — likely bot detection)" if trap else "")
            )

        items: list[dict] = []
        for card in cards:
            link = card.select_one("h3 a[href]")
            if link is None:
                continue
            href = (link.get("href") or "").strip()
            slug = href.rstrip("/").rsplit("/", 1)[-1] if href else ""
            title = " ".join(link.get_text().split())
            if not title or not slug:
                continue

            employment_text = self._chip_text(card, "briefcase")
            job_time, job_type = _parse_employment(employment_text)

            experience_text = self._chip_text(card, "award")
            min_experience_years, max_experience_years = _parse_experience(experience_text)

            posted_text = self._chip_text(card, "calendar-plus")
            published_at = _parse_posted(posted_text)

            deadline_text = self._chip_text(card, "calendar-x")
            deadline = _parse_deadline(deadline_text)

            location, country = _split_location(self._chip_text(card, "map-pin"))

            logo_img = card.select_one("img")
            logo = self._absolute_url((logo_img.get("src") or "").strip()) if logo_img else ""

            items.append(
                {
                    "title": title,
                    "slug": slug,
                    "url": self._absolute_url(href),
                    "company": self._chip_text(card, "building-2"),
                    "location": location,
                    "country": country,
                    "logo": logo,
                    "employment_text": employment_text,
                    "job_time": job_time,
                    "job_type": job_type,
                    "experience_text": experience_text,
                    "min_experience_years": min_experience_years,
                    "max_experience_years": max_experience_years,
                    "posted_text": posted_text,
                    "published_at": published_at.isoformat() if published_at else None,
                    "deadline_text": deadline_text,
                    "deadline": deadline.isoformat() if deadline else None,
                }
            )
        return items

    def _save_detail(self, item: dict, instance: ScrapedItem) -> None:
        """Create/update the GeezJob detail row and link it to the master.

        Persists every field the listing card exposes (the parsed card dict is
        also kept verbatim in ``raw_payload``).
        """
        raw = item.get("raw_data") or {}
        geez, _ = GeezJob.objects.update_or_create(
            external_id=instance.external_id,
            defaults={
                "title": raw.get("title") or item.get("title") or "",
                "slug": raw.get("slug") or "",
                "company": item.get("company") or raw.get("company") or "",
                "company_logo": raw.get("logo") or "",
                "location": item.get("location") or raw.get("location") or "",
                "country": raw.get("country") or "",
                "deadline": item.get("deadline"),
                "deadline_text": raw.get("deadline_text") or "",
                "employment_text": raw.get("employment_text") or "",
                "job_time": raw.get("job_time") or "",
                "job_type": raw.get("job_type") or "",
                "experience_text": raw.get("experience_text") or "",
                "min_experience_years": raw.get("min_experience_years"),
                "max_experience_years": raw.get("max_experience_years"),
                "posted_text": raw.get("posted_text") or "",
                "published_at": item.get("published_at"),
                "url": item.get("url") or "",
                "raw_payload": raw,
                "job_number": instance.job_number,
                "numbered_on": instance.numbered_on,
            },
        )
        if instance.geezjobs_job_id != geez.pk:
            ScrapedItem.objects.filter(pk=instance.pk).update(geezjobs_job=geez)
