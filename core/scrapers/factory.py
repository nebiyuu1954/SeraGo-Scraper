"""ScraperFactory — maps a Source.scraper_type to a scraper class.

Registering a new scraper type is a one-line call, keeping the dispatch
zero-maintenance as new source types are added.
"""
from __future__ import annotations

from core.models import ScraperType, Source

from .base import BaseScraper
from .graphql import GraphQLScraper
from .rest import RestJsonScraper


class ScraperFactory:
    """Registry-driven factory for scraper instances."""

    _registry: dict[str, type[BaseScraper]] = {
        ScraperType.GRAPHQL: GraphQLScraper,
        ScraperType.REST: RestJsonScraper,
    }

    @classmethod
    def register(cls, scraper_type: str, scraper_cls: type[BaseScraper]) -> None:
        cls._registry[scraper_type] = scraper_cls

    @classmethod
    def for_source(cls, source: Source) -> BaseScraper:
        scraper_cls = cls._registry.get(source.scraper_type)
        if scraper_cls is None:
            raise ValueError(f"No scraper registered for type '{source.scraper_type}'")
        return scraper_cls(source)
