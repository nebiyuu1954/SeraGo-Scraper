"""Cloudflare bypass backend registry.

Each backend is a self-contained class that knows:
- its name, API URL, env var for the key, timeout, and credit cost
- how to fetch a page (``fetch`` method)

To add a new anti-bot service:
    1. Create a subclass of ``CloudflareBackend``
    2. Set the class attributes (name, api_url, env_key, etc.)
    3. Implement ``build_request_kwargs`` and ``parse_response``
    4. Call ``register_backend(MyBackend)`` at module level

That's it — the rotation, credit tracking, and env config all pick it up
automatically.  See ``CLOUDFLARE.md`` for the full strategy.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx
from bs4 import BeautifulSoup
from django.conf import settings

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Base class
# ------------------------------------------------------------------


class CloudflareBackend(ABC):
    """Base class for a Cloudflare bypass backend.

    Subclasses set class-level attributes and implement two hooks:
    ``build_request_kwargs`` (the HTTP request args) and ``parse_response``
    (extract the target page's HTML from the API response).
    """

    #: Short slug used in logs, credit tracking, and the registry (e.g. "scrapedo").
    name: str = ""
    #: Full API URL (e.g. "https://api.scrape.do/").
    api_url: str = ""
    #: Django settings attribute name for the API key (e.g. "SCRAPE_DO_API_KEY").
    env_key: str = ""
    #: Default timeout in seconds.
    timeout: float = 120.0
    #: Credits consumed per request (for budget tracking).
    credits_per_request: int = 1
    #: Free-tier credits per month.
    monthly_free_credits: int = 1000
    #: Dashboard URL shown in setup instructions.
    dashboard_url: str = ""
    #: Short description for docs (e.g. "1 credit/req, 1,000 free/mo").
    tier_description: str = ""

    @classmethod
    def get_api_key(cls) -> str:
        """Read the API key from Django settings (env var).

        Strips leading/trailing whitespace and newlines — a common mistake
        when copying keys from dashboards or .env files where a trailing
        newline causes ``Illegal header value`` crashes in httpx.
        """
        raw = getattr(settings, cls.env_key, "") or ""
        return raw.strip()

    @classmethod
    @abstractmethod
    def build_request_kwargs(cls, url: str) -> dict[str, Any]:
        """Return kwargs for ``httpx.get`` or ``httpx.post``.

        Must include ``method``, ``url``, ``timeout``, and either
        ``params``/``json``/``headers`` as needed by the service's API.
        """

    @classmethod
    @abstractmethod
    def parse_response(cls, response: httpx.Response, url: str) -> tuple[str, int]:
        """Extract (target_html, target_status_code) from the API response.

        Raises ``ScrapeError`` on auth/account errors (401/402/403).
        Raises ``httpx.HTTPStatusError`` on transient errors (429/5xx).
        Raises ``CloudflareChallengeError`` if the challenge page leaked through.
        """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Auto-register subclasses that set ``name``."""
        super().__init_subclass__(**kwargs)
        if cls.name and cls.name not in _REGISTRY:
            _REGISTRY[cls.name] = cls


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

_REGISTRY: dict[str, type[CloudflareBackend]] = {}


def register_backend(backend_cls: type[CloudflareBackend]) -> None:
    """Explicitly register a backend (for backends that don't auto-register)."""
    if backend_cls.name:
        _REGISTRY[backend_cls.name] = backend_cls


def get_backend(name: str) -> type[CloudflareBackend] | None:
    return _REGISTRY.get(name)


def all_backends() -> dict[str, type[CloudflareBackend]]:
    """Return a copy of the registry (name -> class)."""
    return dict(_REGISTRY)


def backend_settings(name: str) -> dict[str, Any]:
    """Return {api_url, api_key, timeout} for the named backend."""
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown Cloudflare backend: {name!r}")
    return {
        "api_url": cls.api_url,
        "api_key": cls.get_api_key(),
        "timeout": cls.timeout,
    }


# ------------------------------------------------------------------
# Default rotation order (cheapest first)
# ------------------------------------------------------------------

DEFAULT_ROTATION_ORDER: tuple[str, ...] = (
    "scrapedo",
    "scrapebadger",
    "zenrows",
    "scraperapi",
    "scrapfly",
)

# Relay rotation order for reader-relay sources (e.g. GeezJobs).
# Firecrawl + Jina are cheapest (free), then falls back to CF backends.
RELAY_ROTATION_ORDER: tuple[str, ...] = (
    "firecrawl",
    "jina",
    "scrapedo",
    "scrapebadger",
    "zenrows",
    "scraperapi",
    "scrapfly",
)


# ------------------------------------------------------------------
# Concrete backends — each is a self-contained registration
# ------------------------------------------------------------------


class ScrapeDoBackend(CloudflareBackend):
    """Scrape.do — 1 credit/req, best value. Free: 1,000/mo.

    ``render=true`` executes JS.  Response is raw HTML (200 = success).
    """

    name = "scrapedo"
    api_url = "https://api.scrape.do/"
    env_key = "SCRAPE_DO_API_KEY"
    credits_per_request = 1
    monthly_free_credits = 1000
    dashboard_url = "https://scrape.do/dashboard"
    tier_description = "1 credit/req, 1,000 free/mo"

    @classmethod
    def build_request_kwargs(cls, url: str) -> dict[str, Any]:
        return {
            "method": "GET",
            "url": cls.api_url,
            "timeout": cls.timeout,
            "params": {"token": cls.get_api_key(), "url": url, "render": "true"},
        }

    @classmethod
    def parse_response(cls, response: httpx.Response, url: str) -> tuple[str, int]:
        _check_auth_errors(response, "Scrape.do")
        return _extract_raw_html(response, "Scrape.do", url)


class ScrapeBadgerBackend(CloudflareBackend):
    """ScrapeBadger — 1-3 credits/req. Free: 1,000/mo.

    POST endpoint.  Response is JSON ``{"content": "<html>"}``.
    """

    name = "scrapebadger"
    api_url = "https://scrapebadger.com/v1/web/scrape"
    env_key = "SCRAPEBADGER_API_KEY"
    credits_per_request = 2
    monthly_free_credits = 1000
    dashboard_url = "https://scrapebadger.com/dashboard"
    tier_description = "1-3 credits/req, 1,000 free/mo"

    @classmethod
    def build_request_kwargs(cls, url: str) -> dict[str, Any]:
        return {
            "method": "POST",
            "url": cls.api_url,
            "timeout": cls.timeout,
            "json": {"url": url, "format": "html"},
            "headers": {"x-api-key": cls.get_api_key(), "Content-Type": "application/json"},
        }

    @classmethod
    def parse_response(cls, response: httpx.Response, url: str) -> tuple[str, int]:
        _check_auth_errors(response, "ScrapeBadger", method="POST")
        try:
            payload = response.json()
        except ValueError:
            raise httpx.HTTPStatusError(
                "ScrapeBadger returned a non-JSON body",
                request=httpx.Request("POST", cls.api_url),
                response=response,
            )
        content = payload.get("content") or ""
        if not content:
            raise httpx.HTTPStatusError(
                "ScrapeBadger returned empty content",
                request=httpx.Request("POST", cls.api_url),
                response=response,
            )
        from core.challenge import is_cloudflare_challenge
        if is_cloudflare_challenge(content):
            from core.challenge import CloudflareChallengeError
            raise CloudflareChallengeError(f"Cloudflare challenge page returned for {url}")
        target_status = payload.get("status_code") or response.status_code
        return content, target_status


class ZenRowsBackend(CloudflareBackend):
    """ZenRows — 25 credits/req but 5,000 free/mo (200 requests).

    ``js_render=true`` executes JS; ``premium_proxy=true`` uses residential IPs.
    Response is raw HTML (200 = success).
    """

    name = "zenrows"
    api_url = "https://api.zenrows.com/v1/"
    env_key = "ZENROWS_API_KEY"
    timeout = 120.0
    credits_per_request = 25
    monthly_free_credits = 5000
    dashboard_url = "https://app.zenrows.com/dashboard"
    tier_description = "25 credits/req, 5,000 free/mo (≈200 requests)"

    @classmethod
    def build_request_kwargs(cls, url: str) -> dict[str, Any]:
        return {
            "method": "GET",
            "url": cls.api_url,
            "timeout": cls.timeout,
            "params": {"apikey": cls.get_api_key(), "url": url, "js_render": "true", "premium_proxy": "true"},
        }

    @classmethod
    def parse_response(cls, response: httpx.Response, url: str) -> tuple[str, int]:
        _check_auth_errors(response, "ZenRows")
        return _extract_raw_html(response, "ZenRows", url)


class ScraperAPIBackend(CloudflareBackend):
    """ScraperAPI — 5-75 credits/req. Free: 1,000/mo.

    ``render=true`` executes JS.  Response is raw HTML (200 = success).
    """

    name = "scraperapi"
    api_url = "https://api.scraperapi.com"
    env_key = "SCRAPERAPI_KEY"
    credits_per_request = 25
    monthly_free_credits = 1000
    dashboard_url = "https://dashboard.scraperapi.com/dashboard"
    tier_description = "5-75 credits/req, 1,000 free/mo"

    @classmethod
    def build_request_kwargs(cls, url: str) -> dict[str, Any]:
        return {
            "method": "GET",
            "url": cls.api_url,
            "timeout": cls.timeout,
            "params": {"api_key": cls.get_api_key(), "render": "true", "url": url},
        }

    @classmethod
    def parse_response(cls, response: httpx.Response, url: str) -> tuple[str, int]:
        _check_auth_errors(response, "ScraperAPI")
        return _extract_raw_html(response, "ScraperAPI", url)


class ScrapFlyBackend(CloudflareBackend):
    """ScrapFly — 30-80 credits/req. Free: 1,000/mo.

    ``asp=true`` for anti-bot; ``render_js=true`` for JS.  Returns JSON
    envelope with ``result.content`` + ``result.status_code``.
    """

    name = "scrapfly"
    api_url = "https://api.scrapfly.io/scrape"
    env_key = "SCRAPFLY_API_KEY"
    timeout = 160.0
    credits_per_request = 50
    monthly_free_credits = 1000
    dashboard_url = "https://app.scrapfly.io/dashboard"
    tier_description = "30-80 credits/req, 1,000 free/mo"

    @classmethod
    def build_request_kwargs(cls, url: str) -> dict[str, Any]:
        return {
            "method": "GET",
            "url": cls.api_url,
            "timeout": cls.timeout,
            "params": {"url": url, "key": cls.get_api_key(), "asp": "true", "render_js": "true"},
        }

    @classmethod
    def parse_response(cls, response: httpx.Response, url: str) -> tuple[str, int]:
        from core.challenge import CloudflareChallengeError, is_cloudflare_challenge
        _check_auth_errors(response, "ScrapFly")
        try:
            payload = response.json()
        except ValueError:
            raise httpx.HTTPStatusError(
                "ScrapFly returned a non-JSON body",
                request=httpx.Request("GET", cls.api_url),
                response=response,
            )
        result = payload.get("result") or {}
        content = result.get("content") or ""
        if is_cloudflare_challenge(content):
            raise CloudflareChallengeError(f"Cloudflare challenge page returned for {url}")
        if not result.get("success"):
            raise httpx.HTTPStatusError(
                "ScrapFly scrape failed: "
                f"{result.get('reason') or result.get('error') or 'unknown reason'}",
                request=httpx.Request("GET", cls.api_url),
                response=response,
            )
        target_status = int(result.get("status_code") or response.status_code)
        return content, target_status


# ------------------------------------------------------------------
# Reader relay backend (path-based URL pattern)
# ------------------------------------------------------------------


class JinaReaderBackend(CloudflareBackend):
    """Jina Reader — free relay that fetches any URL and returns raw HTML.

    Unlike the API-based backends above, Jina embeds the target URL in the
    **path** (``r.jina.ai/<encoded-url>``) and uses custom headers for
    format control.  This is the cheapest relay (1 credit/req, ~200/day
    free with a key) and bypasses WAFs that block our IP.

    Free tier: 200 req/day with API key, 20 req/day without.
    """

    name = "jina"
    api_url = "https://r.jina.ai"
    env_key = "JINA_API_KEY"
    timeout = 60.0
    credits_per_request = 1
    monthly_free_credits = 6000  # 200/day × 30 days
    dashboard_url = "https://jina.ai"
    tier_description = "1 credit/req, ~6,000 free/mo (200/day)"

    @classmethod
    def build_request_kwargs(cls, url: str) -> dict[str, Any]:
        from urllib.parse import quote
        # Jina embeds the target URL in the path, not query params.
        # safe='%' keeps already-encoded segments (%XX) intact.
        encoded_url = quote(url, safe='%')
        headers: dict[str, str] = {
            "X-Return-Format": "html",
            "X-No-Cache": "true",
        }
        api_key = getattr(settings, cls.env_key, "") or ""
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return {
            "method": "GET",
            "url": f"{cls.api_url}/{encoded_url}",
            "timeout": cls.timeout,
            "headers": headers,
        }

    @classmethod
    def parse_response(cls, response: httpx.Response, url: str) -> tuple[str, int]:
        # Jina returns 402 when free-tier quota is exhausted
        if response.status_code == 402:
            from core.challenge import ScrapeError
            raise ScrapeError(
                "Jina Reader free-tier quota exhausted. "
                "Set JINA_API_KEY for a higher limit, or wait for quota to reset."
            )
        _check_auth_errors(response, "Jina Reader")
        return _extract_raw_html(response, "Jina Reader", url)


class FirecrawlBackend(CloudflareBackend):
    """Firecrawl — 1 credit/req, 1,000 free/mo. Returns JSON with HTML.

    POST to /v2/scrape with {"url": "...", "formats": ["html"]}.
    Response is JSON: {"success": true, "data": {"html": "..."}}.
    Free tier: 1,000 pages/month, no card required.
    """

    name = "firecrawl"
    api_url = "https://api.firecrawl.dev/v2/scrape"
    env_key = "FIRECRAWL_API_KEY"
    timeout = 60.0
    credits_per_request = 1
    monthly_free_credits = 1000
    dashboard_url = "https://firecrawl.dev"
    tier_description = "1 credit/req, 1,000 free/mo"

    @classmethod
    def build_request_kwargs(cls, url: str) -> dict[str, Any]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        api_key = cls.get_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return {
            "method": "POST",
            "url": cls.api_url,
            "timeout": cls.timeout,
            "json": {"url": url, "formats": ["html"]},
            "headers": headers,
        }

    @classmethod
    def parse_response(cls, response: httpx.Response, url: str) -> tuple[str, int]:
        _check_auth_errors(response, "Firecrawl", method="POST")
        try:
            payload = response.json()
        except ValueError:
            raise httpx.HTTPStatusError(
                "Firecrawl returned a non-JSON body",
                request=httpx.Request("POST", cls.api_url),
                response=response,
            )
        if not payload.get("success"):
            error_msg = payload.get("error") or payload.get("message") or "unknown error"
            raise httpx.HTTPStatusError(
                f"Firecrawl scrape failed: {error_msg}",
                request=httpx.Request("POST", cls.api_url),
                response=response,
            )
        data = payload.get("data") or {}
        content = data.get("html") or ""
        if not content:
            raise httpx.HTTPStatusError(
                "Firecrawl returned empty HTML",
                request=httpx.Request("POST", cls.api_url),
                response=response,
            )
        from core.challenge import CloudflareChallengeError, is_cloudflare_challenge
        if is_cloudflare_challenge(content):
            raise CloudflareChallengeError(f"Cloudflare challenge page returned for {url}")
        return content, response.status_code


# ------------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------------


def _check_auth_errors(
    response: httpx.Response, service_name: str, method: str = "GET",
) -> None:
    """Raise on auth/account errors (401/402/403) and transient blips (429/5xx).

    401/402/403 → ScrapeError (won't self-fix).
    429/5xx → httpx.HTTPStatusError (retried by the caller).
    """
    if response.status_code in (401, 402, 403):
        from core.challenge import ScrapeError
        raise ScrapeError(
            f"{service_name} rejected the request (HTTP {response.status_code}) "
            f"— check the API key and account credits."
        )
    if response.status_code == 429 or response.status_code >= 500:
        raise httpx.HTTPStatusError(
            f"{service_name} returned HTTP {response.status_code}",
            request=httpx.Request("GET", service_name),
            response=response,
        )


def _extract_raw_html(
    response: httpx.Response, service_name: str, url: str,
) -> tuple[str, int]:
    """For backends that return raw HTML: check for Cloudflare challenge, return (html, status)."""
    from core.challenge import CloudflareChallengeError, is_cloudflare_challenge
    if is_cloudflare_challenge(response.text):
        raise CloudflareChallengeError(f"Cloudflare challenge page returned for {url}")
    return response.text, response.status_code
