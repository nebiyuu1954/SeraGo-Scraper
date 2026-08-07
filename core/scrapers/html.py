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

from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from django.utils import timezone

from .base import BaseScraper, transform_parse_datetime

DEFAULT_TIMEOUT = 30.0


class HtmlScraper(BaseScraper):
    """GET/HTML scraper for server-side rendered listing pages."""

    def _page_url(self, page: int) -> str:
        """Absolute URL for the given 0-based page index.

        Page 1 stays the bare endpoint (the site's own pagination omits the
        param on page 1); ``page_1_based`` sources get ``?page=N`` from page 2.
        """
        pagination = self.source.pagination or {}
        key = pagination.get("page_key", "page")
        if pagination.get("page_1_based"):
            number = page + 1
            return self.source.endpoint if number <= 1 else f"{self.source.endpoint}?{key}={number}"
        return self.source.endpoint if page == 0 else f"{self.source.endpoint}?{key}={page}"

    def fetch(self, page: int = 0) -> BeautifulSoup:
        """GET one listing page and return its parsed DOM."""
        url = self._page_url(page)
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            **(self.source.headers or {}),
        }
        response = httpx.get(
            url,
            headers=headers,
            timeout=float((self.source.pagination or {}).get("timeout", DEFAULT_TIMEOUT)),
        )
        # Record the request even when it fails — it still hit the site.
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
