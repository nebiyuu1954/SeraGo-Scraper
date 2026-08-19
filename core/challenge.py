"""Cloudflare challenge detection — shared to avoid circular imports.

Both ``core.scrapers.html`` and ``core.cloudflare_backends`` need to detect
Cloudflare's bot-check interstitial.  This module owns the markers, the
detection function, and ``ScrapeError`` so neither needs to import the other.
"""
from __future__ import annotations


# Substrings that mark Cloudflare's bot-check interstitial ("Just a
# moment...", Turnstile).  A relayed fetch answers with it as HTTP 200 —
# the relay itself got through — so only the BODY can reveal it.
CHALLENGE_MARKERS = (
    "just a moment",
    "challenges.cloudflare.com",
)


class ScrapeError(Exception):
    """A scrape-level error (HTTP failure, auth rejection, bot detection)."""


class CloudflareChallengeError(ScrapeError):
    """The target answered with Cloudflare's bot-check page, not the content."""


def is_cloudflare_challenge(text: str) -> bool:
    """True when the body is Cloudflare's challenge interstitial."""
    lowered = text.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)
