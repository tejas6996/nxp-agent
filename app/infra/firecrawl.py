"""
Firecrawl infrastructure client.

Wraps the firecrawl-py SDK and exposes a clean scraping interface
used by services. All Firecrawl-specific logic is isolated here.
"""

import logging

from firecrawl import Firecrawl

from app.exceptions import FirecrawlClientError
from app.settings import Settings

logger = logging.getLogger(__name__)


class FirecrawlClient:
    """Thin wrapper around the Firecrawl SDK for web content scraping."""

    def __init__(self, settings: Settings) -> None:
        self._client = Firecrawl(api_key=settings.firecrawl_api_key)

    def scrape(self, url: str, max_age: int = 172800000) -> dict:
        """Scrape a URL and return its main content as markdown.

        Args:
            url: The URL to scrape.
            max_age: Maximum cache age in milliseconds (default: 48 hours).

        Returns:
            Dict with at minimum a 'markdown' key containing the page content.

        Raises:
            FirecrawlClientError: If the Firecrawl API returns an error.
        """
        try:
            logger.debug("Scraping URL: %s", url)
            result = self._client.scrape(
                url,
                only_main_content=True,
                max_age=max_age,
                formats=["markdown"],
            )
            # firecrawl-py returns a Document object; normalise to dict
            if hasattr(result, "__dict__"):
                return {k: v for k, v in vars(result).items() if v is not None}
            return result if isinstance(result, dict) else {}
        except Exception as exc:
            logger.error("Firecrawl scrape failed for %s: %s", url, exc)
            raise FirecrawlClientError(f"Firecrawl scrape failed for {url}: {exc}") from exc
