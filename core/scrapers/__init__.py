"""Scraper package: BaseScraper, GraphQLScraper, RestJsonScraper, HtmlScraper, HaHuJobsScraper, GeezJobsScraper, ReporterJobsScraper, ScraperFactory."""
from .base import TRANSFORMS, BaseScraper, ScrapeError
from .factory import ScraperFactory
from .geezjobs import GeezJobsScraper
from .graphql import GraphQLScraper
from .hahujobs import HaHuJobsScraper
from .html import HtmlScraper
from .reporterjobs import ReporterJobsScraper
from .rest import RestJsonScraper

__all__ = [
    "BaseScraper",
    "GeezJobsScraper",
    "GraphQLScraper",
    "HaHuJobsScraper",
    "HtmlScraper",
    "ReporterJobsScraper",
    "RestJsonScraper",
    "ScraperFactory",
    "ScrapeError",
    "TRANSFORMS",
]
