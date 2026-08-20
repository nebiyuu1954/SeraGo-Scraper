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

from .base import DEFAULT_RETRIES, BaseScraper, request_with_retry, transform_parse_datetime
from core.challenge import ScrapeError

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

# Cloudflare challenge detection — imported from core.challenge to avoid
# circular imports with core.cloudflare_backends.  The error class is
# re-exported as a ScrapeError subclass so existing ``isinstance`` checks
# (catching ScrapeError) still work.
from core.challenge import (
    CHALLENGE_MARKERS,
    CloudflareChallengeError as _BaseChallengeError,
    is_cloudflare_challenge,
)

# Also import the base (non-ScrapeError) version for isinstance checks
# against errors raised by cloudflare_backends.parse_response.
from core.challenge import CloudflareChallengeError as _CoreChallengeError


class CloudflareChallengeError(_BaseChallengeError, ScrapeError):
    """The target answered with Cloudflare's bot-check page, not the content."""
    pass

# Base URL of the r.jina.ai reader relay (free tier: ~20 req/min without a
# key, 500 req/min with a free JINA_API_KEY). When a source sets
# ``pagination.relay: "jina"`` its pages are fetched from Jina's own
# infrastructure, so the target site never sees our IP — a WAF that blocks us
# (e.g. GeezJobs' Hostinger edge returning 403) does not block Jina's.
JINA_BASE_URL = "https://r.jina.ai"

# Anti-bot rotation backends — see core.cloudflare_backends for the
# registry and CLOUDFLARE.md for the full strategy.  To add a new
# service, subclass ``CloudflareBackend`` in cloudflare_backends.py
# (no other files need to change).
from core.cloudflare_backends import (
    DEFAULT_ROTATION_ORDER as CLOUDFLARE_ROTATION_ORDER,
    RELAY_ROTATION_ORDER,
    CloudflareBackend,
    all_backends,
    backend_settings,
    get_backend,
)

# Legacy constants kept for backward compatibility (tests import these).
SCRAPFLY_API_URL = "https://api.scrapfly.io/scrape"
SCRAPFLY_TIMEOUT = 160.0
ZENROWS_API_URL = "https://api.zenrows.com/v1/"
ZENROWS_TIMEOUT = 120.0
SCRAPE_DO_API_URL = "https://api.scrape.do/"
SCRAPE_DO_TIMEOUT = 120.0
SCRAPEBADGER_API_URL = "https://scrapebadger.com/v1/web/scrape"
SCRAPEBADGER_TIMEOUT = 120.0
SCRAPERAPI_API_URL = "https://api.scraperapi.com"
SCRAPERAPI_TIMEOUT = 120.0


def _cloudflare_backend_settings(service: str) -> dict:
    """Return {api_url, api_key, timeout} for the given service.

    Delegates to the ``core.cloudflare_backends`` registry.  The old
    hardcoded if/elif chain is replaced by the registry so adding a new
    backend only requires a subclass in ``cloudflare_backends.py``.
    """
    try:
        return backend_settings(service)
    except ValueError:
        raise ScrapeError(f"Unknown Cloudflare backend: {service}")


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


def _classify_relay_error(exc: Exception) -> str:
    """Human-readable one-liner for a relay/backend failure.

    Used in the final error message of relay_rotate and cloudflare_rotate
    to show exactly what went wrong with each backend.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 402:
            return "402 Payment Required (quota exhausted)"
        if code == 403:
            return "403 Forbidden (access denied / WAF block)"
        if code == 429:
            return "429 Too Many Requests (rate limited)"
        if code >= 500:
            return f"{code} Server Error (backend may be down)"
        return f"HTTP {code}"
    if isinstance(exc, ScrapeError):
        msg = str(exc)
        # Truncate long messages but keep the key info
        if len(msg) > 120:
            msg = msg[:117] + "..."
        return msg
    if isinstance(exc, httpx.TransportError):
        # httpx.LocalProtocolError (bad headers) is a TransportError subclass
        name = type(exc).__name__
        msg = str(exc)
        if "Illegal header" in msg:
            return f"{name}: API key has invalid characters (check for trailing newlines/spaces)"
        return f"Transport error: {name}: {msg[:120]}"
    return f"{type(exc).__name__}: {exc}"


def _is_permanent_backend_error(exc: Exception) -> bool:
    """True when the error is permanent and retrying the SAME backend won't help.

    - 402 (quota exhausted) — retrying burns credits, same result
    - Cloudflare challenge — backend can't bypass this site
    - Illegal header (bad API key) — same key won't work next time
    - ScrapeError with quota/auth keywords — backend rejected us permanently

    Retrying wastes time; the rotation should skip to the next backend
    immediately.  This was the #1 cause of the 27-minute GeezJobs run:
    Jina (402 × 3 retries) + ScrapeDo (502 × 3 retries × 60s timeout)
    = 27 minutes of waiting for backends that were never going to work.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        # 402 = quota exhausted, 401 = bad API key
        if code in (402, 401):
            return True
    if isinstance(exc, (CloudflareChallengeError, _CoreChallengeError)):
        return True
    if isinstance(exc, httpx.TransportError):
        msg = str(exc)
        if "Illegal header" in msg:
            return True
    # Jina and other backends raise ScrapeError (not HTTPStatusError) for
    # quota/auth failures — detect by keyword to skip retries immediately.
    if isinstance(exc, ScrapeError):
        msg = str(exc).lower()
        if any(kw in msg for kw in ("quota", "exhausted", "rejected", "check the api key")):
            return True
    return False


def _is_retryable_backend_error(exc: Exception) -> bool:
    """True when retrying the same backend might succeed.

    Only true for transient errors: 502/503/504 (server may recover),
    429 (rate limit, wait helps), transport errors (connection reset).
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    if isinstance(exc, httpx.TransportError):
        return "Illegal header" not in str(exc)
    return False


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
        if relay == "cloudflare_rotate":
            # Smart rotation across all configured Cloudflare bypass backends.
            # Tries each in cheapest-first order, skipping exhausted services.
            # See CLOUDFLARE.md for the full rotation strategy.
            return self._fetch_via_cloudflare_rotate(url, page)
        if relay == "relay_rotate":
            # Reader-relay rotation: tries Jina first (cheapest, free),
            # then falls back to CF backends.  For sources like GeezJobs
            # where Jina is the primary relay but we want fault tolerance.
            return self._fetch_via_relay_rotate(url, page)
        if relay == "scrapfly":
            # Anti-bot backend: bypasses Cloudflare-style protection and
            # renders JS (also fixes the JS-skeleton problem). Needs the
            # SCRAPFLY_API_KEY env var / setting.
            scrapfly_cls = get_backend("scrapfly")
            if scrapfly_cls is None:
                raise ScrapeError("scrapfly backend not registered — check core.cloudflare_backends")
            return self._fetch_via_backend(scrapfly_cls, url, page)
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
                    candidate.status_code in (402, 403, 429)
                    or candidate.status_code >= 500
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
            if (
                isinstance(last_error, httpx.HTTPStatusError)
                and last_error.response is not None
                and last_error.response.status_code == 402
            ):
                raise ScrapeError(
                    "Relay returned HTTP 402 Payment Required on all "
                    f"{attempts} attempt(s) — the API quota is exhausted. "
                    "Fix: (1) set JINA_API_KEY for a higher free-tier limit "
                    "(200 req/day vs 20), or (2) switch to relay_rotate which "
                    "falls back to Scrape.do / ScrapFly when Jina is down, "
                    "or (3) wait for the daily quota reset."
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

        Legacy wrapper — delegates to the ``scrapfly`` backend in the
        ``core.cloudflare_backends`` registry.  Kept for backward compat
        with tests that mock this method directly.
        """
        scrapfly_cls = get_backend("scrapfly")
        if scrapfly_cls is None:
            raise ScrapeError("scrapfly backend not registered — check core.cloudflare_backends")
        api_key = scrapfly_cls.get_api_key()
        if not api_key:
            raise ScrapeError(
                "relay='scrapfly' is configured but SCRAPFLY_API_KEY is not "
                "set — add it to the repo secrets / .env (scrapfly.io "
                "dashboard, free tier: 1,000 credits/month)."
            )
        return self._fetch_via_backend(scrapfly_cls, url, page)

    def _fetch_via_cloudflare_rotate(self, url: str, page: int) -> BeautifulSoup:
        """Smart rotation across all configured Cloudflare bypass backends.

        Tries each backend in ``CLOUDFLARE_ROTATION_ORDER`` (cheapest first),
        skipping services whose API key is missing, whose monthly credits
        are exhausted, or which are *known-broken* for this source (permanent
        errors like 402 quota or CF challenge are cached so subsequent pages
        skip them instantly).

        See CLOUDFLARE.md for the full rotation strategy and credit math.
        """
        # Lazy import to avoid circular imports at module level.
        from core.models import ScraperCreditUsage

        month = timezone.localdate().strftime("%Y-%m")
        source_slug = self.source.slug
        # Per-source broken-backend cache.
        if not hasattr(self, "_broken_backends"):
            self._broken_backends: dict[str, set[str]] = {}
        broken = self._broken_backends.setdefault(source_slug, set())

        tried: list[str] = []
        _relay_failures: list[tuple[str, Exception]] = []
        last_error: Exception | None = None

        for service in CLOUDFLARE_ROTATION_ORDER:
            if service in broken:
                logger.debug("Cloudflare rotate: skipping %s for %s (known broken)", service, source_slug)
                continue
            cfg = _cloudflare_backend_settings(service)
            # Backends with no API key are skipped UNLESS they're free
            # (e.g. Playwright — no key needed, runs locally).
            backend_cls = get_backend(service)
            is_free = backend_cls is not None and getattr(backend_cls, "credits_per_request", 1) == 0
            if not cfg["api_key"] and not is_free:
                logger.debug("Cloudflare rotate: skipping %s (no API key)", service)
                continue
            # Free backends skip credit checks entirely.
            if not is_free:
                remaining = ScraperCreditUsage.remaining_credits(service, month)
                if remaining <= 0:
                    logger.info("Cloudflare rotate: skipping %s (credits exhausted for %s)", service, month)
                    continue
            else:
                remaining = 999_999  # free = unlimited
            tried.append(service)
            try:
                logger.info("Cloudflare rotate: trying %s for %s%s", service, source_slug, f" (remaining credits: ~{remaining})" if not is_free else " (free)")
                soup = self._dispatch_single_backend(service, url, page)
                # Record credit usage on success.
                credits_cost = ScraperCreditUsage.SERVICE_CREDITS_PER_REQUEST.get(service, 1)
                ScraperCreditUsage.objects.create(
                    service=service,
                    credits_used=credits_cost,
                    month=month,
                    source_slug=source_slug,
                )
                logger.info("Cloudflare rotate: %s succeeded for %s", service, source_slug)
                return soup
            except (ScrapeError, httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_error = exc
                _relay_failures.append((service, exc))
                logger.warning("Cloudflare rotate: %s failed for %s: %s", service, source_slug, exc)
                if _is_permanent_backend_error(exc):
                    broken.add(service)
                    logger.info("Cloudflare rotate: marking %s as broken for %s (permanent error)", service, source_slug)
                continue

        # All backends exhausted.
        if not tried:
            raise ScrapeError(
                "relay='cloudflare_rotate' has no configured backends — "
                "set at least one of ZENROWS_API_KEY, SCRAPE_DO_API_KEY, "
                "SCRAPEBADGER_API_KEY, SCRAPFLY_API_KEY, SCRAPERAPI_KEY. "
                "See CLOUDFLARE.md for setup instructions."
            )
        assert last_error is not None
        # Build a concise per-backend failure summary
        backend_details = []
        for svc, err in _relay_failures:
            reason = _classify_relay_error(err)
            backend_details.append(f"  {svc} → {reason}")
        details_str = "\n".join(backend_details)
        raise ScrapeError(
            f"Cloudflare rotation exhausted — {len(tried)} backend(s) tried "
            f"for {source_slug}, all failed:\n{details_str}"
        )

    def _fetch_via_relay_rotate(self, url: str, page: int) -> BeautifulSoup:
        """Reader-relay rotation: Jina first, then CF backends.

        For sources like GeezJobs where Jina is the primary relay (cheapest,
        free) but we want fault tolerance.  Tries Jina first; if it fails
        (402 quota, 403, 5xx), falls back to the Cloudflare bypass backends
        in cheapest-first order.

        Tracks *known-broken* backends per source so subsequent pages skip
        them instantly instead of retrying for 30+ seconds each.  Also
        enforces a per-source time budget (``max_source_seconds``) so one
        slow site can't eat the entire run.

        See CLOUDFLARE.md for the full relay rotation strategy.
        """
        from core.models import ScraperCreditUsage

        month = timezone.localdate().strftime("%Y-%m")
        source_slug = self.source.slug
        # Per-source broken-backend cache: backends that returned permanent
        # errors (402 quota, CF challenge, bad key) are marked broken and
        # skipped on subsequent pages — no retry waste.
        if not hasattr(self, "_broken_backends"):
            self._broken_backends: dict[str, set[str]] = {}
        broken = self._broken_backends.setdefault(source_slug, set())

        tried: list[str] = []
        _relay_failures: list[tuple[str, Exception]] = []
        last_error: Exception | None = None

        for service in RELAY_ROTATION_ORDER:
            if service in broken:
                logger.debug("Relay rotate: skipping %s for %s (known broken)", service, source_slug)
                continue
            cfg = _cloudflare_backend_settings(service)
            # Backends with no API key are skipped UNLESS they're free
            # (e.g. Playwright — no key needed, runs locally).
            backend_cls = get_backend(service)
            is_free = backend_cls is not None and getattr(backend_cls, "credits_per_request", 1) == 0
            if not cfg["api_key"] and not is_free:
                logger.debug("Relay rotate: skipping %s (no API key)", service)
                continue
            # Skip credit checks for free backends and Jina.
            if not is_free and service != "jina":
                remaining = ScraperCreditUsage.remaining_credits(service, month)
                if remaining <= 0:
                    logger.info("Relay rotate: skipping %s (credits exhausted for %s)", service, month)
                    continue
            else:
                remaining = 999_999  # free = unlimited
            tried.append(service)
            try:
                logger.info("Relay rotate: trying %s for %s%s", service, source_slug, " (free)" if is_free else "")
                soup = self._dispatch_single_backend(service, url, page)
                # Record credit usage on success (skip free backends and Jina).
                if not is_free and service != "jina":
                    credits_cost = ScraperCreditUsage.SERVICE_CREDITS_PER_REQUEST.get(service, 1)
                    ScraperCreditUsage.objects.create(
                        service=service,
                        credits_used=credits_cost,
                        month=month,
                        source_slug=source_slug,
                    )
                logger.info("Relay rotate: %s succeeded for %s", service, source_slug)
                return soup
            except (ScrapeError, httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_error = exc
                _relay_failures.append((service, exc))
                logger.warning("Relay rotate: %s failed for %s: %s", service, source_slug, exc)
                # Mark permanently-broken backends so subsequent pages skip them.
                if _is_permanent_backend_error(exc):
                    broken.add(service)
                    logger.info("Relay rotate: marking %s as broken for %s (permanent error)", service, source_slug)
                continue

        # All backends exhausted.
        if not tried:
            raise ScrapeError(
                "relay='relay_rotate' has no configured backends — "
                "set FIRECRAWL_API_KEY or JINA_API_KEY for the primary relay, "
                "plus at least one of ZENROWS_API_KEY, SCRAPE_DO_API_KEY, etc. "
                "for fallback. See CLOUDFLARE.md for setup instructions."
            )
        assert last_error is not None
        # Build a concise per-backend failure summary
        backend_details = []
        for svc, err in _relay_failures:
            reason = _classify_relay_error(err)
            backend_details.append(f"  {svc} → {reason}")
        details_str = "\n".join(backend_details)
        raise ScrapeError(
            f"Relay rotation exhausted — {len(tried)} backend(s) tried "
            f"for {source_slug}, all failed:\n{details_str}"
        )

    def _dispatch_single_backend(self, service: str, url: str, page: int) -> BeautifulSoup:
        """Dispatch to a registered Cloudflare backend.

        Looks up the backend class from the ``core.cloudflare_backends``
        registry, builds the request, executes it with retries, and
        parses the response.  Adding a new backend only requires a
        subclass in ``cloudflare_backends.py`` — no changes here.
        """
        backend_cls = get_backend(service)
        if backend_cls is None:
            raise ScrapeError(f"Unknown Cloudflare backend: {service}")
        return self._fetch_via_backend(backend_cls, url, page)

    def _fetch_via_backend(
        self, backend_cls: type[CloudflareBackend], url: str, page: int,
    ) -> BeautifulSoup:
        """Shared fetch-retry-parse loop for any registered backend.

        The backend class provides ``build_request_kwargs`` (how to call
        the API) and ``parse_response`` (how to extract the target HTML).
        This method owns the retry cadence, error classification, and
        API-call recording — identical for every backend.

        *Permanent* errors (402 quota, bad API key, Cloudflare challenge)
        break out immediately — retrying wastes time and credits. Only
        transient errors (502/503, 429 rate-limit, connection reset) get
        retried.  This is critical for rotation speed: without it, a
        backend that consistently fails burns ``retries × backoff`` seconds
        on every page.
        """
        retries = int((self.source.pagination or {}).get("retries", DEFAULT_RETRIES))
        backoff = float(
            (self.source.pagination or {}).get("relay_backoff_seconds", RELAY_BACKOFF_SECONDS)
        )
        # Source-level timeout overrides the backend's default.
        source_timeout = (self.source.pagination or {}).get("timeout")
        attempts = max(1, retries)
        response = None
        page_html: str | None = None
        target_status: int | None = None
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                # Backends with custom_fetch (e.g. Playwright) bypass httpx
                # entirely — they launch a real browser or use a different
                # transport.  If custom_fetch returns HTML, use it directly;
                # otherwise fall through to the standard httpx path.
                custom_timeout = float(source_timeout) if source_timeout else backend_cls.timeout
                custom_result = backend_cls.custom_fetch(url, custom_timeout)
                if custom_result is not None:
                    html, status = custom_result
                    # No httpx response object — create a minimal one for
                    # the Cloudflare challenge check.
                    page_html = html
                    target_status = status
                    # Run the challenge check on the custom-fetched HTML.
                    from core.challenge import is_cloudflare_challenge
                    if is_cloudflare_challenge(html):
                        raise CloudflareChallengeError(
                            f"Cloudflare challenge page returned for {url}"
                        )
                    break
                req_kwargs = backend_cls.build_request_kwargs(url)
                method = req_kwargs.pop("method", "GET")
                if source_timeout is not None:
                    req_kwargs["timeout"] = float(source_timeout)
                http_fn = httpx.post if method == "POST" else httpx.get
                candidate = http_fn(**req_kwargs)
                html, status = backend_cls.parse_response(candidate, url)
                response = candidate
                page_html = html
                target_status = status
                break
            except (
                httpx.TransportError,
                httpx.HTTPStatusError,
                httpx.LocalProtocolError,
                CloudflareChallengeError,
                _CoreChallengeError,
                ScrapeError,
            ) as exc:
                # Wrap bare CloudflareChallengeError as ScrapeError-compatible
                if isinstance(exc, _CoreChallengeError) and not isinstance(exc, ScrapeError):
                    exc = CloudflareChallengeError(str(exc))
                last_error = exc
                # Don't waste time retrying permanent errors — skip to next
                # backend immediately.  402 (quota), bad API key, CF challenge,
                # etc. won't resolve by retrying the same backend.
                if _is_permanent_backend_error(exc):
                    logger.warning(
                        "%s attempt %d/%d failed (%s) — permanent error, not retrying",
                        backend_cls.name, attempt + 1, attempts, exc,
                    )
                    break
                if attempt < attempts - 1:
                    logger.warning(
                        "%s attempt %d/%d failed (%s) — retrying in %.0fs",
                        backend_cls.name, attempt + 1, attempts, exc,
                        backoff * (attempt + 1),
                    )
                    time.sleep(backoff * (attempt + 1))
        if response is None:
            assert last_error is not None
            if isinstance(last_error, CloudflareChallengeError):
                raise ScrapeError(
                    f"Blocked by Cloudflare challenge even through {backend_cls.name} "
                    f"on all {attempts} attempt(s) — the site's protection "
                    "beat the bypass."
                )
            raise last_error
        assert page_html is not None and target_status is not None
        self._record_api_call(page, target_status)
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
