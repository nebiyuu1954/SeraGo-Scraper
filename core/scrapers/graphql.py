"""GraphQL (Hasura) scraper — used for Afriwork and HaHu Jobs.

Fetches one page via POST with ``query`` + variables, then extracts the
results list at the configured ``results_path`` (e.g. ``data.jobs``).
"""
from __future__ import annotations

import httpx

from .base import BaseScraper, ScrapeError, dig

DEFAULT_PAGE_SIZE = 10
DEFAULT_TIMEOUT = 30.0


class GraphQLScraper(BaseScraper):
    """Paged POST/JSON scraper for Hasura GraphQL endpoints."""

    def _build_payload(self, page: int) -> dict:
        """Build the POST body for a given 0-based page.

        ``limit``/``offset`` are always derived from the page so multi-page
        runs advance correctly; any static values in ``pagination.variables``
        are ignored for these keys. Custom variable names can be configured
        via ``limit_var``/``offset_var`` in the pagination rules.
        """
        pagination = self.source.pagination or {}
        page_size = int(pagination.get("page_size", DEFAULT_PAGE_SIZE))
        offset = page * page_size

        variables = dict(pagination.get("variables") or {})
        variables[pagination.get("limit_var", "limit")] = page_size
        variables[pagination.get("offset_var", "offset")] = offset
        return {"query": self.source.query, "variables": variables}

    def fetch(self, page: int = 0) -> dict:
        payload = self._build_payload(page)
        headers = {"Content-Type": "application/json", **(self.source.headers or {})}

        response = httpx.post(
            self.source.endpoint,
            json=payload,
            headers=headers,
            timeout=float((self.source.pagination or {}).get("timeout", DEFAULT_TIMEOUT)),
        )
        response.raise_for_status()
        return response.json()

    def parse(self, raw: dict) -> list[dict]:
        # Hasura reports GraphQL errors in the body with HTTP 200 — surface them.
        if isinstance(raw, dict) and raw.get("errors"):
            raise ScrapeError(f"GraphQL errors: {raw['errors']}")

        results_path = (self.source.pagination or {}).get("results_path", "data")
        items = dig(raw, results_path)
        if not isinstance(items, list):
            raise ScrapeError(f"Expected a list at '{results_path}', got {type(items).__name__}")
        return items
