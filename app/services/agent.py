"""
GPT-based article extraction service.

Uses OpenAI to parse structured article metadata from raw markdown
scraped from newsroom listing pages and individual article pages.
"""

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

from app.exceptions import OpenAIClientError
from app.infra.openai import OpenAIClient

logger = logging.getLogger(__name__)

# Keywords that must appear in a redirect URL path for it to be accepted.
# Prevents GPT from redirecting to product pages, resource hubs, or homepages.
_NEWS_URL_KEYWORDS = frozenset(
    {"news", "press", "blog", "release", "newsroom", "announcement", "media", "investor"}
)

_LISTING_SYSTEM_PROMPT = """You are a precise data extraction assistant specialising in technology company newsrooms.

Your task is to extract a structured list of news articles from the markdown content of a newsroom or press release listing page.

Output format:
Return ONLY a valid JSON object containing:
  - "articles": an array of article objects (may be empty).
  - "redirect_url": (optional) see redirect rules below.

Each article object must contain exactly these keys:
  - "title": The headline of the article as a plain string. Do not truncate or modify it.
  - "url": The full absolute URL to the article. If the extracted URL is relative, resolve it against the base URL provided. If resolution is not possible, omit the entry.
  - "date": The publication date of the article in YYYY-MM-DD format. If a date is present but in another format, convert it. If no date is visible for the article, use null.

Inclusion rules:
  - Include: news articles, press releases, product announcements, executive statements, earnings releases, and technology blog posts.
  - Exclude: navigation links, breadcrumbs, pagination controls, social media share buttons, category/tag labels, footer links, cookie notices, search widgets, and any link that does not point to a discrete article.

Redirect rules:
  - If after careful analysis you find NO articles on this page, and the page content visibly contains a hyperlink that leads directly to a newsroom, press releases, blogs, or news listing sub-page, set "redirect_url" to that absolute URL.
  - CRITICAL: The redirect_url must be a URL that literally appears as a hyperlink in the markdown content provided to you. Do NOT use any URL from your training data or prior knowledge. Do NOT guess or construct a URL. If the exact URL is not present in the content, omit this field entirely.
  - Only use redirect_url when articles is an empty array and a specific news listing link is explicitly visible in the content.
  - Do NOT set redirect_url to a generic homepage, search page, social media page, or broad category hub. It must point to a page that directly lists individual articles or press releases.

Quality rules:
  - Do not include duplicate URLs.
  - Do not fabricate titles, URLs, or redirect_url. Only use values explicitly present in the content.

Do not include any explanation, commentary, or markdown outside of the JSON object."""

_ARTICLE_SYSTEM_PROMPT = """You are a precise content extraction assistant specialising in technology news articles.

Your task is to extract structured data from the markdown content of a single technology news article page.

Output format:
Return ONLY a valid JSON object with exactly these keys:
  - "title": The article headline as a plain string. Use the main H1 or the most prominent heading. Do not include the site name or section labels.
  - "date": The article publication date in YYYY-MM-DD format. Look for a byline, dateline, or metadata tag. Use null if not found.
  - "content": The first 300 words of the main article body as clean, readable prose. Preserve the natural sentence and paragraph flow. Do not truncate mid-sentence.
  - "image_url": The absolute URL of the primary or featured image (hero image or Open Graph image). Prefer the largest or most prominent image associated with the article content. Use null if no image is present.

Exclusion rules for "content":
  - Do not include: page title, author name, publication date line, section headers, navigation menus, related articles, social sharing prompts, newsletter sign-up text, cookie banners, legal disclaimers, or any boilerplate text that is not part of the article body.

Quality rules:
  - Do not paraphrase or summarise the content. Extract the actual text verbatim.
  - Do not fabricate any field. If a value cannot be determined from the content, use null.
  - image_url must be an absolute URL starting with http:// or https://. Do not return relative paths.

Do not include any explanation, commentary, or markdown outside of the JSON object."""


async def extract_articles_from_listings(
    client: OpenAIClient,
    listing_results: list[dict[str, Any]],
    semaphore: asyncio.Semaphore,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Extract article metadata from scraped listing page markdown using GPT.

    Args:
        client: Initialised OpenAI client.
        listing_results: Output of crawling.scrape_listing_pages.
        semaphore: Semaphore controlling max parallel OpenAI calls.

    Returns:
        Tuple of (articles, redirect_hints).
        articles: Flat list of article dicts with keys: source_name, title, url, date.
        redirect_hints: List of {source_name, redirect_url} for sites that returned no articles
            but suggested a more specific listing URL to follow.
        Listings that fail GPT extraction are skipped with a warning.
    """

    async def _extract_one(
        listing: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
        async with semaphore:
            parsed = urlparse(listing["listing_url"])
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            user_content = (
                f"Base URL: {base_url}\n\nMarkdown:\n{listing['markdown'][:24000]}"
            )
            try:
                data = await asyncio.to_thread(
                    client.complete_json, _LISTING_SYSTEM_PROMPT, user_content
                )
                articles: list[dict[str, Any]] = data.get("articles", [])
                for article in articles:
                    article["source_name"] = listing["source_name"]

                redirect_hint: dict[str, str] | None = None
                if not articles:
                    raw_redirect = data.get("redirect_url", "")
                    # Strip URL fragment anchors that break scraping
                    redirect_url = raw_redirect.split("#")[0].rstrip("/") if raw_redirect else ""
                    if redirect_url and redirect_url.startswith("http"):
                        url_lower = redirect_url.lower()
                        if any(kw in url_lower for kw in _NEWS_URL_KEYWORDS):
                            redirect_hint = {
                                "source_name": listing["source_name"],
                                "redirect_url": redirect_url,
                            }
                            logger.info(
                                "No articles from %s. Redirect accepted: %s",
                                listing["source_name"],
                                redirect_url,
                            )
                        else:
                            logger.info(
                                "No articles from %s. Redirect rejected (non-news URL): %s",
                                listing["source_name"],
                                redirect_url,
                            )

                logger.info(
                    "Extracted %d articles from %s listing.",
                    len(articles),
                    listing["source_name"],
                )
                return articles, redirect_hint
            except (OpenAIClientError, KeyError, ValueError) as exc:
                logger.warning(
                    "Skipping listing extraction for %s: %s",
                    listing["source_name"],
                    exc,
                )
                return [], None

    tasks = [asyncio.create_task(_extract_one(listing)) for listing in listing_results]
    results = await asyncio.gather(*tasks)

    all_articles: list[dict[str, Any]] = []
    redirect_hints: list[dict[str, str]] = []
    for articles, hint in results:
        all_articles.extend(articles)
        if hint:
            redirect_hints.append(hint)

    return all_articles, redirect_hints


async def extract_article_contents(
    client: OpenAIClient,
    scraped_articles: list[dict[str, Any]],
    semaphore: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    """Extract structured content from scraped article page markdown using GPT.

    Args:
        client: Initialised OpenAI client.
        scraped_articles: Output of crawling.scrape_articles.
        semaphore: Semaphore controlling max parallel OpenAI calls.

    Returns:
        List of article dicts with keys: source_name, url, title, date, content, image_url.
        Articles that fail extraction are skipped with a warning.
    """

    async def _extract_one(article: dict[str, Any]) -> dict[str, Any] | None:
        async with semaphore:
            try:
                data = await asyncio.to_thread(
                    client.complete_json,
                    _ARTICLE_SYSTEM_PROMPT,
                    article["markdown"][:12000],
                )
                return {
                    "source_name": article["source_name"],
                    "url": article["url"],
                    "title": data.get("title") or article.get("title", "Untitled"),
                    "date": data.get("date") or article.get("date"),
                    "content": data.get("content", ""),
                    "image_url": data.get("image_url"),
                }
            except (OpenAIClientError, KeyError, ValueError) as exc:
                logger.warning(
                    "Skipping content extraction for %s: %s", article["url"], exc
                )
                return None

    tasks = [asyncio.create_task(_extract_one(a)) for a in scraped_articles]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]
