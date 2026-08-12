"""HTML (server-side rendered) scraper base.

Used for websites with no JSON API — the listings are baked into the HTML
(e.g. GeezJobs' ``.opportunity-card`` divs on /search-jobs). The generic part
here handles:

* :meth:`fetch` — GET one page with browser-like headers, returning a
  BeautifulSoup document. Page 1 is the bare endpoint; the ``?page=N`` param
  is only added from page 2 on when ``page_1_based`` is set (matching the
  site's own pagination links — GeezJobs page 1 is ``/search-jobs`` and page 2
  is ``/search-jobs?page=2``).
* the client-side today filter (:meth:`_keep_item` / :meth:`_past_today_boundary`)
  for sites whose cards only offer a relative ``Posted: X ago`` timestamp: the
  listings arrive newest-first, so once a page contains no items from today
  the sweep can stop (identical semantics to ``RestJsonScraper``).

Site-specific scraping lives in a per-slug subclass: :meth:`parse` turns the
soup into a list of raw item dicts and ``_save_detail()`` writes the per-site
detail row. The generic ``HtmlScraper`` cannot be scraped directly — its
:meth:`parse` raises ``NotImplementedError``.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone

from .base import DEFAULT_RETRIES, BaseScraper, request_with_retry, transform_parse_datetime

DEFAULT_TIMEOUT = 30.0

# Base URL of the r.jina.ai reader relay (free tier: ~20 req/min without a
# key, 500 req/min with a free JINA_API_KEY). When a source sets
# ``pagination.relay: "jina"`` its pages are fetched from Jina's own
# infrastructure, so the target site never sees our IP — a WAF that blocks us
# (e.g. GeezJobs' Hostinger edge returning 403) does not block Jina's.
JINA_BASE_URL = "https://r.jina.ai"

# Full and abbreviated month names for the shared date-text parser below.
# (GeezJobs shows 'September 7, 2026'; Ethiopian Reporter Jobs shows
# 'August 5, 2026' — same shape, different sites.)
MONTHS = {
    "January": 1, "Jan": 1,
    "February": 2, "Feb": 2,
    "March": 3, "Mar": 3,
    "April": 4, "Apr": 4,
    "May": 5,
    "June": 6, "Jun": 6,
    "July": 7, "Jul": 7,
    "August": 8, "Aug": 8,
    "September": 9, "Sep": 9, "Sept": 9,
    "October": 10, "Oct": 10,
    "November": 11, "Nov": 11,
    "December": 12, "Dec": 12,
}


def parse_month_day_year(text: str) -> datetime | None:
    """Extract a 'Month D, YYYY' date anywhere in the text -> aware local midnight.

    Handles full and abbreviated month names ('September 7, 2026' / 'Aug 7,
    2026'); returns None when no recognizable date is present. Used for the
    absolute deadline/posted dates both HTML sites show on their cards.
    """
    if not text:
        return None
    match = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text)
    if not match:
        return None
    month = MONTHS.get(match.group(1).capitalize())
    if month is None:
        return None
    try:
        return timezone.make_aware(datetime(int(match.group(3)), month, int(match.group(2))))
    except ValueError:
        return None


class HtmlScraper(BaseScraper):
    """GET/HTML scraper for server-side rendered listing pages."""

    def _page_url(self, page: int) -> str:
        """Absolute URL for the given 0-based page index.

        Page 1 stays the bare endpoint (the site's own pagination omits the
        param on page 1) in both styles:

        * query style (default): ``?page=N`` from page 2 when ``page_1_based``
          is set (GeezJobs: ``/search-jobs`` -> ``/search-jobs?page=2``).
        * path style (WordPress): ``page_style: "path"`` builds
          ``/page/N/`` URLs from page 2 (Ethiopian Reporter Jobs:
          ``/jobs-in-ethiopia/`` -> ``/jobs-in-ethiopia/page/2/``).
        """
        pagination = self.source.pagination or {}
        key = pagination.get("page_key", "page")
        if pagination.get("page_style") == "path":
            number = page + 1
            if number <= 1:
                return self.source.endpoint
            base = self.source.endpoint.rstrip("/")
            return f"{base}/page/{number}/"
        if pagination.get("page_1_based"):
            number = page + 1
            return self.source.endpoint if number <= 1 else f"{self.source.endpoint}?{key}={number}"
        return self.source.endpoint if page == 0 else f"{self.source.endpoint}?{key}={page}"

    def _relay_url(self, url: str) -> str:
        """Route a target page URL through the configured relay (currently r.jina.ai).

        The target URL — including its query string — is percent-encoded and
        embedded in the relay's own path, so pagination (``?page=N``) stays
        part of the target instead of leaking onto the relay's request.
        Returns the URL unchanged when no relay is configured.
        """
        relay = (self.source.pagination or {}).get("relay")
        if relay == "jina":
            # safe='%' keeps already-encoded segments (%XX) intact while still
            # encoding ?, &, = etc. — the relay path must never double-encode.
            return f"{JINA_BASE_URL}/{quote(url, safe='%')}"
        return url

    def fetch(self, page: int = 0) -> BeautifulSoup:
        """GET one listing page and return its parsed DOM."""
        url = self._relay_url(self._page_url(page))
        relay = (self.source.pagination or {}).get("relay")
        if relay == "jina":
            # Fetch through the r.jina.ai reader, which requests the target
            # site itself. The source's browser headers are deliberately NOT
            # forwarded: the relay's own WAF 403s requests carrying a browser
            # User-Agent (a bot-avoidance heuristic). We ask for fresh raw
            # HTML (the site-specific parse needs the markup, not markdown)
            # and send the free JINA_API_KEY (settings/.env) when configured.
            headers = {
                "X-Return-Format": "html",
                "X-No-Cache": "true",
            }
            # settings already reads JINA_API_KEY from the environment at import.
            api_key = getattr(settings, "JINA_API_KEY", "") or ""
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                **(self.source.headers or {}),
            }
        response = request_with_retry(
            httpx.get,
            url=url,
            headers=headers,
            timeout=float((self.source.pagination or {}).get("timeout", DEFAULT_TIMEOUT)),
            retries=int((self.source.pagination or {}).get("retries", DEFAULT_RETRIES)),
        )
        # Record the request even when it fails — it still hit the site (or relay).
        # With a relay, the logged http_status is the RELAY's response (e.g. 200
        # even when the target site 403s); parse() still fails loudly on
        # bot-check/error pages, so a blocked target doesn't read as success.
        self._record_api_call(page, response.status_code)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    def parse(self, raw: BeautifulSoup) -> list[dict]:
        """HTML sites are site-specific — subclasses must implement this.

        Register the concrete scraper per-slug in ``ScraperFactory`` (e.g.
        ``GeezJobsScraper``) so its parse is the one that runs.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement parse(); HTML sites are "
            "site-specific (register a per-slug scraper in ScraperFactory)."
        )

    # -- client-side today filter (listings arrive newest-first) --

    def _today_start(self) -> datetime:
        """Aware datetime for the start of the current local day."""
        return timezone.make_aware(datetime.combine(timezone.localdate(), datetime.min.time()))

    def _is_today_item(self, item: dict) -> bool:
        """True when the item was published today (local time)."""
        date_filter = (self.source.pagination or {}).get("date_filter") or {}
        field = date_filter.get("field") or "published_at"
        published = transform_parse_datetime(item.get(field))
        if published is None:
            # No date at all: keep it (safer than silently dropping).
            return True
        return published >= self._today_start()

    def _keep_item(self, item: dict) -> bool:
        """Drop items published before today when the today filter is on."""
        if not self.only_today:
            return True
        return self._is_today_item(item)

    def _past_today_boundary(self, page: int, items: list[dict]) -> bool:
        """Stop when this page has already moved past today's listings.

        The feed cannot be filtered by date server-side, but returns listings
        newest-first, so if a page contains NO items from today, every page
        below it is older than today too and the sweep can end. (Mixed pages
        are swept through and their pre-today items dropped by
        :meth:`_keep_item`.)
        """
        if not items:
            return True  # everything kept was pre-today -> past the boundary
        return all(not self._is_today_item(i) for i in items)
