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

import logging
import re
import time
from datetime import datetime
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone

from .base import DEFAULT_RETRIES, BaseScraper, ScrapeError, request_with_retry, transform_parse_datetime

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0

#: Backoff (seconds) between EXTRA attempts when the Jina relay answers a
#: relayed fetch with a transient HTTP error (403/429/5xx). The relay is
#: shared infrastructure and blips even when the target site is fine, so we
#: wait ~3s/6s before giving up rather than failing the run on the first blip.
RELAY_BACKOFF_SECONDS = 3.0

#: Extra attempts (beyond the source's transport ``retries``) for relayed
#: fetches hit by a transient relay HTTP error. Tunable per source via
#: ``pagination.relay_retries``.
DEFAULT_RELAY_RETRIES = 2

#: Substrings that mark Cloudflare's bot-check interstitial ("Just a
#: moment...", Turnstile). A relayed fetch answers with it as HTTP 200 — the
#: relay itself got through — so only the BODY can reveal it. The target site
#: may serve it to every requester (Under Attack / strict managed challenge),
#: or only sometimes; either way the scraper must never mistake it for the
#: real page. Keep the list tight: Cloudflare-fronted sites routinely include
#: ``/cdn-cgi/challenge-platform`` scripts on REAL pages, so that path is
#: deliberately NOT a marker — only the interstitial's own title and the
#: Turnstile origin are.
CHALLENGE_MARKERS = (
    "just a moment",
    "challenges.cloudflare.com",
)


class CloudflareChallengeError(ScrapeError):
    """The target answered with Cloudflare's bot-check page, not the content."""


def is_cloudflare_challenge(text: str) -> bool:
    """True when the body is Cloudflare's challenge interstitial."""
    lowered = text.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)

# Base URL of the r.jina.ai reader relay (free tier: ~20 req/min without a
# key, 500 req/min with a free JINA_API_KEY). When a source sets
# ``pagination.relay: "jina"`` its pages are fetched from Jina's own
# infrastructure, so the target site never sees our IP — a WAF that blocks us
# (e.g. GeezJobs' Hostinger edge returning 403) does not block Jina's.
JINA_BASE_URL = "https://r.jina.ai"

# ScrapFly anti-bot scraping API (pagination.relay="scrapfly"). Unlike the
# free Jina relay, it actively bypasses anti-scraping systems (asp) and
# renders JavaScript in a cloud browser (render_js) — the right backend for
# sites behind a Cloudflare challenge (e.g. Ethiopian Reporter Jobs) and for
# JS-rendered listings. The response is the JSON envelope (result.content
# holds the target's rendered HTML, result.status_code the target's status)
# — proxified_response was tried but the target's challenge consistently
# beat it, while the JSON envelope returns the real page.
SCRAPFLY_API_URL = "https://api.scrapfly.io/scrape"
#: ScrapFly's own read timeout is 155s (asp + render_js can take a while);
#: the client must not cut it short. Tunable per source via ``pagination.timeout``.
SCRAPFLY_TIMEOUT = 160.0

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

    Handles full and abbreviated month names, with or without a trailing
    period on the abbreviation ('September 7, 2026' / 'Aug 7, 2026' / 'Sep. 6,
    2026' — GeezJobs' detail page abbreviates with a period); returns None
    when no recognizable date is present. Used for the absolute
    deadline/posted dates both HTML sites show on their cards.
    """
    if not text:
        return None
    match = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})", text)
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
        url = self._page_url(page)
        relay = (self.source.pagination or {}).get("relay")
        if relay == "scrapfly":
            # Anti-bot backend: bypasses Cloudflare-style protection and
            # renders JS (also fixes the JS-skeleton problem). Needs the
            # SCRAPFLY_API_KEY env var / setting.
            return self._fetch_via_scrapfly(url, page)
        url = self._relay_url(url)
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
        timeout = float((self.source.pagination or {}).get("timeout", DEFAULT_TIMEOUT))
        retries = int((self.source.pagination or {}).get("retries", DEFAULT_RETRIES))
        # The free relay pool is flaky: 403/429/5xx blips happen even when the
        # target site is fine (the relay is shared infra — not the site's own
        # WAF). ``request_with_retry`` only retries transport errors, so a
        # relayed fetch gets these EXTRA attempts for transient HTTP statuses,
        # with the same growing backoff. Non-relayed fetches keep the old
        # behavior (transport errors only, retried ``retries`` times).
        relay_retries = int(
            (self.source.pagination or {}).get("relay_retries", DEFAULT_RELAY_RETRIES)
        )
        # Per-source backoff between EXTRA attempts (default RELAY_BACKOFF_SECONDS).
        backoff = float(
            (self.source.pagination or {}).get(
                "relay_backoff_seconds", RELAY_BACKOFF_SECONDS
            )
        )
        attempts = max(1, retries + (relay_retries if relay else 0))
        response = None
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                candidate = request_with_retry(
                    httpx.get,
                    url=url,
                    headers=headers,
                    timeout=timeout,
                    retries=1,  # the loop below owns the retry cadence
                )
                if relay and (
                    candidate.status_code in (403, 429) or candidate.status_code >= 500
                ):
                    raise httpx.HTTPStatusError(
                        f"Relay returned HTTP {candidate.status_code}",
                        request=httpx.Request("GET", url),
                        response=candidate,
                    )
                if is_cloudflare_challenge(candidate.text):
                    # The target answered with its Cloudflare challenge page
                    # (the relay got through — hence HTTP 200). Treat it like
                    # a transient failure and retry with backoff: the block
                    # often lifts, and a fresh relay attempt may egress from a
                    # different IP. If every attempt is challenged, the run
                    # fails below with a message that names Cloudflare.
                    raise CloudflareChallengeError(
                        f"Cloudflare challenge page returned for {url}"
                    )
                response = candidate
                break
            except (
                httpx.TransportError,
                httpx.HTTPStatusError,
                CloudflareChallengeError,
            ) as exc:
                last_error = exc
                if attempt < attempts - 1:
                    logger.warning(
                        "HTTP attempt %d/%d failed (%s) — retrying in %.0fs",
                        attempt + 1,
                        attempts,
                        exc,
                        backoff * (attempt + 1),
                    )
                    time.sleep(backoff * (attempt + 1))
        if response is None:
            # Every attempt failed — raise so the run fails loudly (the day log
            # records a failed run instead of a silent empty success).
            assert last_error is not None
            if isinstance(last_error, CloudflareChallengeError):
                raise ScrapeError(
                    "Blocked by Cloudflare challenge ('Just a moment') — the "
                    f"site served its bot-check page on all {attempts} "
                    "attempt(s). This is the site blocking automated access "
                    "(the relay IPs are flagged); it usually clears on its "
                    "own — verify the page opens in a normal browser."
                )
            raise last_error
        # Record the request even when it fails — it still hit the site (or relay).
        # With a relay, the logged http_status is the RELAY's response (e.g. 200
        # even when the target site 403s); parse() still fails loudly on
        # bot-check/error pages, so a blocked target doesn't read as success.
        self._record_api_call(page, response.status_code)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    def _fetch_via_scrapfly(self, url: str, page: int) -> BeautifulSoup:
        """Fetch a page through the ScrapFly anti-bot API.

        ``asp=true`` turns on anti-scraping bypass (Cloudflare challenges,
        WAFs); ``render_js=true`` executes the site's JavaScript in a cloud
        browser (so JS-rendered listings arrive as real cards). The JSON
        envelope carries the target's rendered HTML in ``result.content`` and
        the target's own status in ``result.status_code``.

        Requires ``settings.SCRAPFLY_API_KEY`` (env var SCRAPFLY_API_KEY).
        Account/auth errors (401/402/403) fail immediately; transient blips
        (429/5xx) and an unbypassed Cloudflare challenge are retried with
        backoff, then fail loudly.
        """
        api_key = getattr(settings, "SCRAPFLY_API_KEY", "") or ""
        if not api_key:
            raise ScrapeError(
                "relay='scrapfly' is configured but SCRAPFLY_API_KEY is not "
                "set — add it to the repo secrets / .env (scrapfly.io "
                "dashboard, free tier: 1,000 credits/month)."
            )
        params = {
            "url": url,
            "key": api_key,
            "asp": "true",
            "render_js": "true",
        }
        timeout = float((self.source.pagination or {}).get("timeout", SCRAPFLY_TIMEOUT))
        retries = int((self.source.pagination or {}).get("retries", DEFAULT_RETRIES))
        backoff = float(
            (self.source.pagination or {}).get(
                "relay_backoff_seconds", RELAY_BACKOFF_SECONDS
            )
        )
        attempts = max(1, retries)
        response = None
        page_html: str | None = None
        target_status: int | None = None
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                candidate = httpx.get(SCRAPFLY_API_URL, params=params, timeout=timeout)
                if candidate.status_code in (401, 402, 403):
                    # Auth/account problems won't fix themselves — fail now.
                    raise ScrapeError(
                        f"ScrapFly rejected the request (HTTP "
                        f"{candidate.status_code}) — check SCRAPFLY_API_KEY "
                        "and account credits."
                    )
                if candidate.status_code == 429 or candidate.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"ScrapFly returned HTTP {candidate.status_code}",
                        request=httpx.Request("GET", SCRAPFLY_API_URL),
                        response=candidate,
                    )
                try:
                    payload = candidate.json()
                except ValueError as exc:
                    raise httpx.HTTPStatusError(
                        "ScrapFly returned a non-JSON body",
                        request=httpx.Request("GET", SCRAPFLY_API_URL),
                        response=candidate,
                    ) from exc
                result = payload.get("result") or {}
                content = result.get("content") or ""
                if is_cloudflare_challenge(content):
                    # asp did not beat the target's challenge — retry a fresh
                    # attempt, then fail loudly below if it persists.
                    raise CloudflareChallengeError(
                        f"Cloudflare challenge page returned for {url}"
                    )
                if not result.get("success"):
                    # Intermittent: asp occasionally loses to the target's
                    # WAF ("Forbidden") even though the next attempt
                    # succeeds — retry with backoff like a transient blip.
                    raise httpx.HTTPStatusError(
                        "ScrapFly scrape failed: "
                        f"{result.get('reason') or result.get('error') or 'unknown reason'}",
                        request=httpx.Request("GET", SCRAPFLY_API_URL),
                        response=candidate,
                    )
                response = candidate
                # The target's rendered HTML + its OWN status, so the rest of
                # the pipeline (parse, _record_api_call) sees a normal page
                # response.
                page_html = content
                target_status = int(result.get("status_code") or candidate.status_code)
                break
            except (
                httpx.TransportError,
                httpx.HTTPStatusError,
                CloudflareChallengeError,
            ) as exc:
                last_error = exc
                if attempt < attempts - 1:
                    logger.warning(
                        "ScrapFly attempt %d/%d failed (%s) — retrying in %.0fs",
                        attempt + 1,
                        attempts,
                        exc,
                        backoff * (attempt + 1),
                    )
                    time.sleep(backoff * (attempt + 1))
        if response is None:
            assert last_error is not None
            if isinstance(last_error, CloudflareChallengeError):
                raise ScrapeError(
                    "Blocked by Cloudflare challenge even through ScrapFly's "
                    f"anti-bot proxy (asp) on all {attempts} attempt(s) — the "
                    "site's protection beat the bypass; check the ScrapFly "
                    "dashboard for the request logs."
                )
            raise last_error
        assert page_html is not None and target_status is not None
        # The recorded status is the TARGET's own (result.status_code) — so
        # the day log shows the page's real response (200 = the site served
        # the feed, even though the API wrapper itself returned 200 either way).
        self._record_api_call(page, target_status)
        response.raise_for_status()
        return BeautifulSoup(page_html, "html.parser")

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
