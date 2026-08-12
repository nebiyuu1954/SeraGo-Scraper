"""Ethiopian Reporter Jobs HTML scraper — the fifth website (WordPress / Noo).

Ethiopian Reporter Jobs (``www.ethiopianreporterjobs.com``) is a WordPress job
board using the Noo Job Board theme. Like GeezJobs there is **no JSON API** —
the listings are server-rendered ``article.noo_job`` cards on
``/jobs-in-ethiopia/`` — so it reuses the generic ``HtmlScraper`` pipeline and
adds the site-specific parts. What makes it different from GeezJobs:

* **Pagination**: WordPress path style — page 1 is the bare
  ``/jobs-in-ethiopia/`` and page 2 is ``/jobs-in-ethiopia/page/2/``
  (``page_style: \"path\"`` in the source's pagination config, handled by
  ``HtmlScraper._page_url``). The archive ends when WordPress redirects to
  ``/expired`` with zero cards, which stops the sweep naturally.

* **Exact timestamps**: every card carries ``<time class=\"entry-date\"
  datetime=\"2026-08-05T06:00:53+03:00\">``, so ``published_at`` is EXACT (no
  estimation like GeezJobs' relative chips). The posted/closing date spans
  (``.job-date__posted`` / ``.job-date__closing``) hold the display text; the
  closing span is the deadline and is parsed with the shared
  ``parse_month_day_year`` helper.

* **Batch publishing**: the newspaper posts jobs in batches (the current feed
  is one large August 5 batch, with a pinned \"featured\" widget repeating the
  same ~7 jobs at the top of every page). Dedup by post id handles the
  repeats; the strict today-only filter (``only_today``) means a run on a
  non-posting day truthfully stores nothing.

* **Anti-bot**: the site is behind Cloudflare. A challenge page has no cards,
  no archive container (``div.jobs.posts-loop``) AND none of the site's
  framing (``header``/``nav``), so ``parse()`` raises :class:`ScrapeError`
  instead of silently recording an empty feed. A real WordPress page with no
  cards but the site header intact (e.g. the ``/expired`` page deep archive
  pages redirect to) is a legitimately exhausted feed and returns ``[]``.

* **Job type**: the card shows a single phrase ("Full Time" / "Contract" /
  ...); the raw text is stored on ``ReporterJob`` (``job_type_text``) with the
  site-normalized value (``job_type``), while the master
  ``ScrapedItem.job_type`` carries the shared ``JobType`` enum value.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from core.models import JobType, ReporterJob, ReporterScrapeLog, ScrapedItem

from .base import ScrapeError, transform_parse_datetime
from .html import HtmlScraper, parse_month_day_year

#: The shared JobType values the site's type chip can map onto (lowercased,
#: with spaces as underscores: full_time, part_time, contract, ...). Chips
#: outside this set map to '' so the master ScrapedItem.job_type never gets an
#: invalid enum value.
_JOB_TYPE_VALUES = {choice.value.lower() for choice in JobType}


def _normalize_job_type(text: str) -> str:
    """"Full Time" -> "full_time" — a shared JobType value, or '' for unknown chips.

    Parenthetical qualifiers ("Full Time (Remote)") are stripped; the raw
    chip text is preserved on ``ReporterJob.job_type_text`` either way.
    """
    if not text:
        return ""
    cleaned = re.sub(r"\(.*?\)", "", text)
    value = " ".join(cleaned.split()).strip().lower().replace(" ", "_")
    return value if value in _JOB_TYPE_VALUES else ""


class ReporterJobsScraper(HtmlScraper):
    """Ethiopian Reporter Jobs — WordPress Noo-theme HTML cards, /page/N/ pagination."""

    site_log_model = ReporterScrapeLog
    #: Per-site detail model + the OneToOne link on ScrapedItem — lets the
    #: shared batch-save path upsert every detail row in one statement.
    detail_model = ReporterJob
    detail_fk_field = "reporter_job"

    #: Anchor for the listings archive — its presence means we got a real
    #: listings page (as opposed to a Cloudflare challenge / error page).
    _ARCHIVE_ANCHOR_SELECTOR = "div.jobs.posts-loop"
    #: Any WordPress site framing (header/nav). A page that still has it but no
    #: cards and no archive container is a legitimately exhausted feed (the
    #: deep archive pages redirect to /expired, which keeps the site header);
    #: only a page with NO framing at all is treated as a bot challenge.
    _SITE_FRAMING_SELECTOR = "header, nav"

    @staticmethod
    def _card_text(card: Tag, selector: str) -> str:
        """Cleaned text of the first matching node inside a card, or ''."""
        node = card.select_one(selector)
        return " ".join(node.get_text().split()) if node else ""

    def parse(self, raw: BeautifulSoup) -> list[dict]:
        """Extract one raw card dict per ``article.noo_job`` on the page."""
        cards = raw.select("article.noo_job")
        if not cards and not raw.select(self._ARCHIVE_ANCHOR_SELECTOR):
            # No cards AND no archive container. That is a Cloudflare
            # challenge / bot-check / error page ONLY when the site's own
            # framing is also missing (challenge pages are bare). A real
            # WordPress page with no cards (e.g. the /expired page deep
            # archive pages redirect to) just means the feed is exhausted:
            # return [] and let the sweep stop cleanly.
            if not raw.select(self._SITE_FRAMING_SELECTOR):
                raise ScrapeError(
                    "No job cards, listings archive, or site framing found on "
                    "the page (possible Cloudflare challenge / bot detection)"
                )

        items: list[dict] = []
        for card in cards:
            link = card.select_one("h3.loop-item-title a[href]")
            if link is None:
                continue
            href = (link.get("href") or "").strip()
            detail_url = (card.get("data-url") or "").strip() or href
            post_id = detail_url.rstrip("/").rsplit("/", 1)[-1] if detail_url else ""
            title = " ".join(link.get_text().split())
            if not title or not post_id:
                continue

            time_node = card.select_one("time.entry-date")
            posted_text = ""
            published_at = None
            deadline_text = ""
            deadline = None
            if time_node is not None:
                posted_node = time_node.select_one(".job-date__posted")
                if posted_node is not None:
                    posted_text = " ".join(posted_node.get_text().split())

                datetime_attr = (time_node.get("datetime") or "").strip()
                if datetime_attr:
                    # Exact timestamp from the <time datetime> attribute.
                    published_at = transform_parse_datetime(datetime_attr)
                if published_at is None and posted_text:
                    # Fallback: the display text alone ("August 5, 2026").
                    published_at = parse_month_day_year(posted_text)

                closing_node = time_node.select_one(".job-date__closing")
                if closing_node is not None:
                    raw_closing = " ".join(closing_node.get_text().split())
                    deadline_text = raw_closing.lstrip("-").strip()
                    deadline = parse_month_day_year(deadline_text)

            job_type_text = self._card_text(card, ".job-type")

            items.append(
                {
                    "post_id": post_id,
                    "title": title,
                    "url": detail_url,
                    "company": self._card_text(card, ".job-company"),
                    "job_type_text": job_type_text,
                    "job_type": _normalize_job_type(job_type_text),
                    "location": self._card_text(card, ".job-location"),
                    "posted_text": posted_text,
                    "published_at": published_at.isoformat() if published_at else None,
                    "deadline_text": deadline_text,
                    "deadline": deadline.isoformat() if deadline else None,
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
