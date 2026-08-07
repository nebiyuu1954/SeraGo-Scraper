"""Scraper package: BaseScraper, GraphQLScraper, RestJsonScraper, HtmlScraper, HaHuJobsScraper, GeezJobsScraper, ScraperFactory."""
from .base import TRANSFORMS, BaseScraper, ScrapeError
from .factory import ScraperFactory
from .geezjobs import GeezJobsScraper
from .graphql import GraphQLScraper
from .hahujobs import HaHuJobsScraper
from .html import HtmlScraper
from .rest import RestJsonScraper

__all__ = [
    "BaseScraper",
    "GeezJobsScraper",
    "GraphQLScraper",
    "HaHuJobsScraper",
    "HtmlScraper",
    "RestJsonScraper",
    "ScraperFactory",
    "ScrapeError",
    "TRANSFORMS",
]
