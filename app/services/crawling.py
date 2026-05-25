"""
Async Firecrawl scraping service.

Provides concurrent scraping of listing pages and individual article pages
using asyncio semaphores to control parallelism against the Firecrawl API.
"""

import asyncio
import logging
from typing import Any

from app.exceptions import FirecrawlClientError
from app.infra.firecrawl import FirecrawlClient

logger = logging.getLogger(__name__)


async def scrape_listing_pages(
    client: FirecrawlClient,
    sites: list[dict[str, str]],
    semaphore: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    """Scrape all newsroom listing pages concurrently.

    Args:
        client: Initialised Firecrawl client.
        sites: List of dicts with 'name' and 'url' keys.
        semaphore: Semaphore controlling max parallel Firecrawl requests.

    Returns:
        List of dicts with keys: source_name, listing_url, markdown.
        Sites that fail are omitted and a warning is logged.
    """

    async def _scrape_one(site: dict[str, str]) -> dict[str, Any] | None:
        async with semaphore:
            try:
                result = await asyncio.to_thread(client.scrape, site["url"])
                logger.info("Scraped listing page: %s", site["name"])
                return {
                    "source_name": site["name"],
                    "listing_url": site["url"],
                    "markdown": result.get("markdown", ""),
                }
            except FirecrawlClientError as exc:
                logger.warning(
                    "Skipping listing page for %s (%s): %s",
                    site["name"],
                    site["url"],
                    exc,
                )
                return None

    tasks = [asyncio.create_task(_scrape_one(site)) for site in sites]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]


async def scrape_articles(
    client: FirecrawlClient,
    articles: list[dict[str, Any]],
    semaphore: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    """Scrape individual article pages concurrently.

    Args:
        client: Initialised Firecrawl client.
        articles: List of article dicts containing at minimum 'url' and 'source_name'.
        semaphore: Semaphore controlling max parallel Firecrawl requests.

    Returns:
        List of dicts with keys: source_name, url, title, date, markdown.
        Failed articles are omitted and a warning is logged.
    """

    async def _scrape_one(article: dict[str, Any]) -> dict[str, Any] | None:
        async with semaphore:
            try:
                result = await asyncio.to_thread(client.scrape, article["url"])
                logger.debug("Scraped article: %s", article["url"])
                return {
                    "source_name": article["source_name"],
                    "url": article["url"],
                    "title": article.get("title", ""),
                    "date": article.get("date"),
                    "markdown": result.get("markdown", ""),
                }
            except FirecrawlClientError as exc:
                logger.warning("Skipping article %s: %s", article["url"], exc)
                return None

    tasks = [asyncio.create_task(_scrape_one(a)) for a in articles]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]
