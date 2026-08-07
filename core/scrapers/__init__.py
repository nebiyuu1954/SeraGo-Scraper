"""Scraper package: BaseScraper, GraphQLScraper, RestJsonScraper, HaHuJobsScraper, ScraperFactory."""
from .base import TRANSFORMS, BaseScraper, ScrapeError
from .factory import ScraperFactory
from .graphql import GraphQLScraper
from .hahujobs import HaHuJobsScraper
from .rest import RestJsonScraper

__all__ = [
    "BaseScraper",
    "GraphQLScraper",
    "HaHuJobsScraper",
    "RestJsonScraper",
    "ScraperFactory",
    "ScrapeError",
    "TRANSFORMS",
]
