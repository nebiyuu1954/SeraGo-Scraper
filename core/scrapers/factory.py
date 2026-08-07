"""ScraperFactory — maps a Source to a scraper class.

Dispatch happens on the source's ``scraper_type`` (graphql / rest / html /
...), with a per-slug override registry for websites that share a
scraper_type but need their own site-specific detail saving (e.g. HaHuJobs
reuses the GraphQL pipeline but writes its own HaHuJob detail rows +
HaHuScrapeLog, GeezJobs reuses the HTML pipeline with its own GeezJob rows +
GeezScrapeLog, and Ethiopian Reporter Jobs does the same with ReporterJob +
ReporterScrapeLog).
"""
from __future__ import annotations

from core.models import ScraperType, Source

from .base import BaseScraper
from .geezjobs import GeezJobsScraper
from .graphql import GraphQLScraper
from .hahujobs import HaHuJobsScraper
from .html import HtmlScraper
from .reporterjobs import ReporterJobsScraper
from .rest import RestJsonScraper


class ScraperFactory:
    """Registry-driven factory for scraper instances."""

    _registry: dict[str, type[BaseScraper]] = {
        ScraperType.GRAPHQL: GraphQLScraper,
        ScraperType.REST: RestJsonScraper,
        ScraperType.HTML: HtmlScraper,
    }

    #: Per-slug overrides, checked before the scraper_type registry. A
    #: second GraphQL site (or second REST/HTML site) with its own per-site
    #: detail/log models registers its scraper class here.
    _slug_registry: dict[str, type[BaseScraper]] = {
        "hahujobs": HaHuJobsScraper,
        "geezjobs": GeezJobsScraper,
        "reporterjobs": ReporterJobsScraper,
    }

    @classmethod
    def register(cls, scraper_type: str, scraper_cls: type[BaseScraper]) -> None:
        cls._registry[scraper_type] = scraper_cls

    @classmethod
    def for_source(cls, source: Source) -> BaseScraper:
        scraper_cls = cls._slug_registry.get(source.slug) or cls._registry.get(
            source.scraper_type
        )
        if scraper_cls is None:
            raise ValueError(f"No scraper registered for type '{source.scraper_type}'")
        return scraper_cls(source)
