"""Ethiopian Reporter Jobs HTML scraper — the fifth website (WordPress / Careerfy).

Ethiopian Reporter Jobs (``www.ethiopianreporterjobs.com``) is a WordPress job
board. In mid-2026 it switched themes from Noo Job Board to **Careerfy** (the
JobSearch theme), which changed the listing markup completely: cards are now
``div.jobsearch-joblisting-classic-wrap`` inside ``li.jobsearch-column-12`` on
``/jobs-in-ethiopia/``, titles live in ``h2.jobsearch-pst-title a``, and the
job-type badge is ``a.jobsearch-option-btn``. Like GeezJobs there is **no JSON
API** — the listings are in the rendered HTML — so it reuses the generic
``HtmlScraper`` pipeline. What makes it different:

* **Anti-bot**: the site's Cloudflare now challenges every free access path
  (direct requests, the r.jina.ai relay, even headless browsers), so the
  source fetches through the **ScrapFly** anti-bot API (``pagination.relay:
  "scrapfly"`` — see ``HtmlScraper._fetch_via_scrapfly``), which bypasses the
  challenge and renders the page's JavaScript.

* **Relative timestamps**: the new cards only say ``Published X hours ago``
  (no ``<time datetime>``), so ``published_at`` is ESTIMATED like GeezJobs'
  relative chips — good enough for the today filter and ordering.

* **No deadlines**: the theme has the deadline field disabled for these
  listings, so ``deadline`` is left unset and the shared default-deadline
  rule applies (published + 30 days, flagged ``deadline_is_default``); the
  daily report's "Defaulted deadlines" section surfaces them.

* **No server-side pagination**: the theme loads further pages via an AJAX
  "Load more" button, so the sweep is capped at page 1 (``max_pages: 1``) —
  the newest page always carries every same-day posting, which is all the
  today-filtered daily run needs.

* **Job type**: the badge shows a single phrase ("Full-time" / "Contract" /
  ...); the raw text is stored on ``ReporterJob`` (``job_type_text``) with the
  site-normalized value (``job_type``), while the master
  ``ScrapedItem.job_type`` carries the shared ``JobType`` enum value.

* **Anti-bot backstop**: ``parse()`` still treats a page with no cards AND no
  site framing (``header``/``nav``) as a bot/error page and raises
  :class:`ScrapeError` instead of silently recording an empty feed, and a
  page with the framing intact but a JS-required skeleton is a failed fetch,
  not an empty feed.
"""
from __future__ import annotations

import re
from datetime import timedelta

from bs4 import BeautifulSoup, Tag
from django.utils import timezone

from core.models import JobType, ReporterJob, ReporterScrapeLog, ScrapedItem

from .base import ScrapeError
from .html import HtmlScraper

#: The shared JobType values the site's type badge can map onto (lowercased,
#: with spaces/hyphens as underscores: full_time, part_time, contract, ...).
#: Badges outside this set map to '' so the master ScrapedItem.job_type never
#: gets an invalid enum value.
_JOB_TYPE_VALUES = {choice.value.lower() for choice in JobType}

#: The Careerfy cards only carry a RELATIVE posting time ("Published 7 hours
#: ago") — no exact timestamp, unlike the old Noo-theme cards' <time datetime>.
_POSTED_RE = re.compile(r"Published\s*:?\s*(\d+)\s*(min|hour|day|week)s?\s*ago", re.IGNORECASE)


def _normalize_job_type(text: str) -> str:
    """"Full-time" -> "full_time" — a shared JobType value, or '' for unknown badges.

    Parenthetical qualifiers ("Full-time (Remote)") are stripped and both
    spaces and hyphens become underscores (the Careerfy badge is
    "Full-time" / "Part-time"). The raw badge text is preserved on
    ``ReporterJob.job_type_text`` either way.
    """
    if not text:
        return ""
    cleaned = re.sub(r"\(.*?\)", "", text)
    value = (
        " ".join(cleaned.split()).strip().lower().replace(" ", "_").replace("-", "_")
    )
    return value if value in _JOB_TYPE_VALUES else ""


def _parse_published(text: str) -> str | None:
    """'Published 7 hours ago' -> an ESTIMATED ISO timestamp (now − offset).

    The Careerfy cards expose no exact posting time, so like GeezJobs' chips
    the timestamp is estimated from the relative text. Returns None when the
    text carries no recognizable offset.
    """
    match = _POSTED_RE.search(text or "")
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    now = timezone.now()
    if unit.startswith("min"):
        delta = timedelta(minutes=amount)
    elif unit.startswith("hour"):
        delta = timedelta(hours=amount)
    elif unit.startswith("day"):
        delta = timedelta(days=amount)
    elif unit.startswith("week"):
        delta = timedelta(weeks=amount)
    else:
        return None
    return (now - delta).isoformat()


class ReporterJobsScraper(HtmlScraper):
    """Ethiopian Reporter Jobs — WordPress Noo-theme HTML cards, /page/N/ pagination."""

    site_log_model = ReporterScrapeLog
    #: Per-site detail model + the OneToOne link on ScrapedItem — lets the
    #: shared batch-save path upsert every detail row in one statement.
    detail_model = ReporterJob
    detail_fk_field = "reporter_job"

    #: One Careerfy listing card (title, company, location, posted, type
    #: badge). Its presence means we got a real listings page (as opposed to a
    #: Cloudflare challenge / error page).
    _CARD_SELECTOR = "div.jobsearch-joblisting-classic-wrap"
    #: Anchor for the listings archive — same element as the cards (a real
    #: listings page always carries at least one card).
    _ARCHIVE_ANCHOR_SELECTOR = "div.jobsearch-joblisting-classic-wrap"
    #: Any WordPress site framing (header/nav). A page that still has it but no
    #: cards is a legitimately exhausted feed (the theme keeps the site header
    #: on empty archives); only a page with NO framing at all is treated as a
    #: bot challenge.
    _SITE_FRAMING_SELECTOR = "header, nav"
    #: When a fetch returns the raw page without rendering its JavaScript, the
    #: page keeps the site's header/nav but carries zero cards and this
    #: <noscript> warning where the cards should be. That skeleton must NOT
    #: read as an exhausted feed (it would log a silent success and store
    #: nothing for the day) — it is a scrape error.
    _JS_REQUIRED_MARKER = "javascript enabled"

    @staticmethod
    def _card_text(card: Tag, selector: str) -> str:
        """Cleaned text of the first matching node inside a card, or ''."""
        node = card.select_one(selector)
        return " ".join(node.get_text().split()) if node else ""

    def parse(self, raw: BeautifulSoup) -> list[dict]:
        """Extract one raw card dict per Careerfy listing card on the page."""
        cards = raw.select(self._CARD_SELECTOR)
        if not cards:
            # No cards. That is a Cloudflare challenge / bot-check / error
            # page ONLY when the site's own framing is also missing (challenge
            # pages are bare). A real WordPress page with no cards just means
            # the feed is exhausted: return [] and let the sweep stop cleanly.
            # An exception is the JS-rendered skeleton (header + <noscript>
            # "enable javascript" warning, no cards): that is a failed fetch,
            # not an empty feed, so it raises instead of silently logging
            # success with nothing stored.
            body_text = raw.get_text(" ", strip=True).lower()
            if self._JS_REQUIRED_MARKER in body_text:
                raise ScrapeError(
                    "Page returned the JS-required skeleton (no cards, "
                    "javascript not rendered) — retrying is pointless until "
                    "the relay returns the rendered feed"
                )
            if not raw.select(self._SITE_FRAMING_SELECTOR):
                # Collect diagnostic info for the daily report
                title = raw.title.get_text(strip=True) if raw.title else "(no title)"
                html_str = str(raw)
                size_kb = len(html_str) / 1024
                snippet = html_str[:200].replace("\n", " ")
                raise ScrapeError(
                    f"No job cards (selector: {self._CARD_SELECTOR}) and no site "
                    f"framing (header/nav) found on the page. "
                    f"Page title: '{title}', size: {size_kb:.1f}KB. "
                    f"First 200 chars: '{snippet}' "
                    f"Possible causes: Cloudflare challenge leaked through, "
                    f"page layout changed, or JS did not render. "
                    f"Verify the page opens in a real browser."
                )

        items: list[dict] = []
        for card in cards:
            link = card.select_one("h2.jobsearch-pst-title a[href]")
            if link is None:
                continue
            href = (link.get("href") or "").strip()
            # Stable WordPress post id from data-job-id, else the last URL
            # segment (the new /jobs/<id>/ URLs carry it).
            post_id = (card.select_one(".jobsearch-pst-title").get("data-job-id") or "").strip()
            if not post_id:
                post_id = href.rstrip("/").rsplit("/", 1)[-1] if href else ""
            title = " ".join(link.get_text().split())
            if not title or not post_id:
                continue

            # The posted line sits in the <li> holding the calendar icon:
            # "Published 7 hours ago" (the new theme has no exact timestamp).
            posted_text = ""
            published_at = None
            calendar_li = card.select_one("i.jobsearch-calendar")
            if calendar_li is not None:
                li = calendar_li.find_parent("li")
                if li is not None:
                    posted_text = " ".join(li.get_text().split())
            if posted_text:
                published_at = _parse_published(posted_text)

            # The theme has the deadline field disabled for these listings —
            # no deadline anywhere on the card. Leave it unset so the shared
            # default-deadline rule applies (+30 days, flagged); the daily
            # report surfaces it under "Defaulted deadlines".

            # The type badge ("Full-time") sits in the card's userlist cell.
            job_type_text = self._card_text(card, "a.jobsearch-option-btn")

            company = self._card_text(card, "li.job-company-name").lstrip("@").strip()
            location = self._card_text(card, "li:has(i.jobsearch-maps-and-flags)")

            items.append(
                {
                    "post_id": post_id,
                    "title": title,
                    "url": href,
                    "company": company,
                    "job_type_text": job_type_text,
                    "job_type": _normalize_job_type(job_type_text),
                    "location": location,
                    "posted_text": posted_text,
                    "published_at": published_at,
                    "deadline_text": "",
                    "deadline": None,
                }
            )
        return items

    def _detail_defaults(self, item: dict, instance: ScrapedItem) -> dict:
        """The ReporterJob field values for a listing (a faithful mirror of the raw card dict)."""
        raw = item.get("raw_data") or {}
        return {
            "title": raw.get("title") or item.get("title") or "",
            "url": item.get("url") or raw.get("url") or "",
            "company": item.get("company") or raw.get("company") or "",
            "location": item.get("location") or raw.get("location") or "",
            "job_type_text": raw.get("job_type_text") or "",
            "job_type": raw.get("job_type") or "",
            "posted_text": raw.get("posted_text") or "",
            "published_at": item.get("published_at"),
            "deadline_text": raw.get("deadline_text") or "",
            "deadline": item.get("deadline"),
            "raw_payload": raw,
            "job_number": instance.job_number,
            "numbered_on": instance.numbered_on,
        }

    def _save_detail(self, item: dict, instance: ScrapedItem) -> None:
        """Create/update the ReporterJob detail row and link it to the master.

        Persists every field the listing card exposes (the parsed card dict is
        also kept verbatim in ``raw_payload``).
        """
        reporter, _ = ReporterJob.objects.update_or_create(
            external_id=instance.external_id,
            defaults=self._detail_defaults(item, instance),
        )
        if instance.reporter_job_id != reporter.pk:
            ScrapedItem.objects.filter(pk=instance.pk).update(reporter_job=reporter)
