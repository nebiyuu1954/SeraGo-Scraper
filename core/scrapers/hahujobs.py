"""HaHuJobs GraphQL scraper — the third website (job aggregator).

HaHuJobs is a Hasura GraphQL API (``graph.aggregator.hahu.jobs``) that
aggregates listings from many Ethiopian sources (telegram channels, the
Addis Zemen gazette, hahujobs_enterprise, ethiojobs, ...). It reuses the
generic ``GraphQLScraper`` pipeline — fetch/parse/normalize/paginate are all
config-driven through the seeded Source row — and this module only adds the
site-specific bits:

* the per-website day log (``HaHuScrapeLog``),
* skipping listings sourced from ethiojobs: we scrape EthioJobs directly,
  so they would otherwise be stored twice (different external_ids). The
  filter lives in ``save_items`` rather than ``_keep_item`` so the sweep's
  page logic still sees the full feed (an all-ethiojobs page must NOT look
  like an empty page — the generic empty-page stop assumes date ordering,
  which doesn't apply to a source-based filter).
* deriving a clean city location from the nested ``job_cities`` list (the
  dotted-path field mapping cannot reach into lists),
* constructing the job-detail URL (``https://www.hahu.jobs/jobs/<id>`` —
  verified live; the /job/ variant 404s), and
* persisting the ``HaHuJob`` detail row for every stored master item.

"Today" is scoped server-side via ``approved_on`` (see seed_sources.py):
the query filters ``approved_on: {_gte: $from, _lt: $to}`` with the
local-day window the GraphQLScraper injects from the date_filter rules.
"""
from __future__ import annotations

from core.models import HaHuJob, HaHuScrapeLog, ScrapedItem

from .graphql import GraphQLScraper


class HaHuJobsScraper(GraphQLScraper):
    """HaHuJobs aggregator — the GraphQL pipeline plus site-specific detail."""

    site_log_model = HaHuScrapeLog
    #: Per-site detail model + the OneToOne link on ScrapedItem — lets the
    #: shared batch-save path upsert every detail row in one statement.
    detail_model = HaHuJob
    detail_fk_field = "hahujobs_job"

    @staticmethod
    def _city_names(raw: dict) -> list[str]:
        """City names from the nested ``job_cities`` list (job_cities[].city.name)."""
        names = []
        for entry in raw.get("job_cities") or []:
            if not isinstance(entry, dict):
                continue
            city = entry.get("city") or {}
            if city.get("name"):
                names.append(city["name"])
        return names

    def normalize(self, raw_item: dict) -> dict:
        """Config-driven normalize, then fix the list-shaped fields.

        ``location`` lives inside a list (``job_cities[].city.name``) that
        dotted-path mapping cannot reach, so the city names are joined here
        (falling back to the API's free-text location). ``salary`` is coerced
        to a string for the master ``ScrapedItem.salary`` column.
        """
        item = super().normalize(raw_item)
        cities = self._city_names(raw_item)
        if cities:
            item["location"] = ", ".join(cities)
        elif not item.get("location"):
            item["location"] = " ".join(str(raw_item.get("location") or "").split())
        if item.get("salary") is not None:
            item["salary"] = str(item["salary"])
        # The API payload has no detail URL, but the site's job pages live at
        # https://www.hahu.jobs/jobs/<id> (verified live with a real browser).
        if not item.get("url") and raw_item.get("id"):
            item["url"] = f"https://www.hahu.jobs/jobs/{raw_item['id']}"
        return item

    @staticmethod
    def _is_ethiojobs_sourced(item: dict) -> bool:
        """True when the aggregator sourced this listing from ethiojobs."""
        raw = item.get("raw_data") or {}
        return (raw.get("source") or "") == "ethiojobs"

    def save_items(self, items: list[dict]) -> tuple[int, int, int, list[str]]:
        """Save everything except ethiojobs-sourced listings.

        Filtering here (not in ``_keep_item``) keeps the sweep's pagination
        and stop logic honest: a page made entirely of ethiojobs listings
        still counts as a non-empty page, and ethiojobs ids never appear in
        the incremental-stop boundary (they are never stored). The dropped
        listings are reported as ``skipped``.
        """
        kept, dropped = [], []
        for item in items:
            if self._is_ethiojobs_sourced(item):
                dropped.append(item)
            else:
                kept.append(item)
        inserted, updated, skipped, errors = super().save_items(kept)
        return inserted, updated, skipped + len(dropped), errors

    def _detail_defaults(self, item: dict, instance: ScrapedItem) -> dict:
        """The HaHuJob field values for a listing (a faithful mirror of the raw payload)."""
        raw = item.get("raw_data") or {}
        entity = raw.get("entity") or {}
        sub_sector = raw.get("sub_sector") or {}
        sector = sub_sector.get("sector") or {}
        area = raw.get("area") or {}
        isco = raw.get("isco_08") or {}
        soc = raw.get("soc_2010") or {}
        return {
            "title": raw.get("title") or item.get("title") or "",
            "description": item.get("description") or "",
            "type": item.get("job_type") or "",
            "years_of_experience": raw.get("years_of_experience"),
            "max_years_of_experience": raw.get("max_years_of_experience"),
            "salary": item.get("salary"),
            "deadline": item.get("deadline"),
            "expired": bool(raw.get("expired")),
            "location": raw.get("location") or "",
            "source": raw.get("source") or "",
            "application_method": raw.get("application_method") or "",
            "application_url": raw.get("application_url") or "",
            "application_email": raw.get("application_email") or "",
            "number_of_applicants": raw.get("number_of_applicants"),
            "approved_on": item.get("published_at"),
            "total_web_view_count": raw.get("total_web_view_count") or 0,
            "telegram_view_count": raw.get("telegram_view_count") or 0,
            "total_view_count": raw.get("total_view_count") or 0,
            "entity_id": entity.get("id") or "",
            "entity_name": entity.get("name") or "",
            "entity_logo": entity.get("logo") or "",
            "sector_id": sector.get("id") or "",
            "sector_name": sector.get("name") or "",
            "sector_icon_class": sector.get("icon_class") or "",
            "sector_icon_code": sector.get("icon_code") or "",
            "sub_sector_name": sub_sector.get("name") or "",
            "area_name": area.get("name") or "",
            "area_address": area.get("address") or "",
            "isco_08_code": isco.get("isco_08_code") or "",
            "isco_08_title_en": isco.get("title_en") or "",
            "isco_08_title_am": isco.get("title_am") or "",
            "soc_2010_title": soc.get("title") or "",
            "soc_2010_onetsoc_code": soc.get("onetsoc_code") or "",
            "esco_code": raw.get("esco_code") or "",
            "cities": self._city_names(raw),
            "raw_payload": raw,
            "job_number": instance.job_number,
            "numbered_on": instance.numbered_on,
        }

    def _save_detail(self, item: dict, instance: ScrapedItem) -> None:
        """Create/update the HaHuJob detail row and link it to the master.

        Persists every field the HaHuJobs API returns for a listing, so the
        per-site model is a faithful mirror of the raw response (the raw JSON
        is also kept verbatim in ``raw_payload``).
        """
        hahu, _ = HaHuJob.objects.update_or_create(
            external_id=instance.external_id,
            defaults=self._detail_defaults(item, instance),
        )
        if instance.hahujobs_job_id != hahu.pk:
            ScrapedItem.objects.filter(pk=instance.pk).update(hahujobs_job=hahu)
