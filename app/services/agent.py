"""
GPT-based article extraction service.

The model only *selects* information that is present on the page:

* Listing pages are given as a link digest (see services.parsing). The model returns the
  numeric IDs of article links; URLs are resolved from the page itself, so a link in the
  digest can never be invented or mangled.
* Titles returned by the model are verified against the page text and fall back to the
  page's own anchor text when they cannot be found verbatim.
"""

import logging
import re
from datetime import date, timedelta
from typing import Any

from app.exceptions import OpenAIClientError
from app.infra.openai import OpenAIClient
from app.models import ArticleListing
from app.services.parsing import (
    ArticlePage,
    LinkDigest,
    collapse_ws,
    normalize_for_match,
    parse_date,
    url_key,
)

logger = logging.getLogger(__name__)

_GENERIC_LINK_TEXT = re.compile(
    r"^(read|learn|see|view|find out|discover|click)( (more|full|the|article|release|story|here|details))*\W*$",
    re.IGNORECASE,
)

_LISTING_SYSTEM_PROMPT = """You are a precise data extraction assistant specialising in technology company newsrooms.

You receive the text of a newsroom or press-release listing page. Every hyperlink on the page is written as [link text](#N), where N is the link's numeric ID.

Task: list every news item shown on the page.

For each item return:
  - "link_id": the ID of the link that opens that item's own page. Prefer the headline link; if the headline itself is not a link, use the item's "Read more"/"Learn more" link. Use only IDs that appear in the text.
  - "title": the item's headline copied exactly as written on the page - same words, same order, same capitalisation. Do not rephrase, shorten, translate or complete it. Remove only text that is not part of the headline itself, such as a category label, date, "Press Release", "News", or "Read more" that shares the same link.
    The same link ID can appear in several places (e.g. a promotional banner/carousel with a marketing slogan AND the news list). Always take the title from the news-list entry - normally the text of the link itself - never from a banner slogan.
  - "date": the item's publication date as YYYY-MM-DD if the page shows one for that item, otherwise null. Today's date is given; use it to resolve relative dates such as "2 days ago" and dates shown without a year (choose the most recent such date that is not in the future).

Include: press releases, news articles, product announcements, executive and corporate announcements, financial/earnings releases, research briefs and technology blog posts that the page lists as news items.
Exclude: navigation and menu links, category/tag/filter links, pagination, "see all"/archive links, social media links, footer links, product or solution pages, job postings, cookie notices, and anything that is not a discrete news item.

"listing_link_id": only if the page lists NO news items but clearly contains a link to the page that lists them (e.g. "All press releases"), give that link's ID. Otherwise null.

Never invent items, titles, dates or IDs. If unsure whether something is a news item, leave it out."""

_LISTING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "articles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "link_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "date": {"type": ["string", "null"]},
                },
                "required": ["link_id", "title", "date"],
                "additionalProperties": False,
            },
        },
        "listing_link_id": {"type": ["integer", "null"]},
    },
    "required": ["articles", "listing_link_id"],
    "additionalProperties": False,
}

_ARTICLE_SYSTEM_PROMPT = """You are a precise content extraction assistant specialising in technology news articles.

You receive the cleaned text of a single news article page plus some metadata.

Return:
  - "title": the article headline exactly as written on the page (normally the H1). Do not include the site name or section labels. Do not rephrase.
  - "date": the article's publication date as YYYY-MM-DD, from the dateline, byline or metadata. null if not found.
  - "summary": the opening of the main article body, about 300 words, copied verbatim. Preserve the natural sentence and paragraph flow and end at a sentence boundary. Do not paraphrase or summarise. Separate paragraphs with a blank line.

Exclude from "summary": the headline, author name, author/analyst biographies, the date line on its own, section headers, navigation, related articles, share prompts, newsletter sign-up text, cookie banners, image captions, contact details, "About <company>" boilerplate and legal boilerplate. A dateline at the start of the first paragraph (e.g. "SAN JOSE, Calif., Oct. 1, 2026 -") may be kept.

If the text does not contain the article body itself (e.g. an error page, a list of links, or only an author biography / company description), return an empty summary. Never invent content."""

_ARTICLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "date": {"type": ["string", "null"]},
        "summary": {"type": "string"},
    },
    "required": ["title", "date", "summary"],
    "additionalProperties": False,
}


def _clean_date(value: Any, today: date) -> str | None:
    parsed = parse_date(value)
    if parsed is None or parsed > today + timedelta(days=1) or parsed.year < 1990:
        return None
    return parsed.isoformat()


_MONTHS = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
# A publication date glued to the end of a headline, e.g. "... performance Oct 2, 2026".
_TRAILING_DATE = re.compile(
    rf"[\s|\-–—,]*(?:{_MONTHS}\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}\s+{_MONTHS}\s+\d{{4}}"
    rf"|\d{{4}}[-./]\d{{1,2}}[-./]\d{{1,2}})\s*$",
    re.IGNORECASE,
)


def _strip_trailing_date(title: str) -> str:
    stripped = _TRAILING_DATE.sub("", title).strip()
    return stripped if len(stripped.split()) >= 3 else title


def _compact(text: str) -> str:
    """Normalised text without any whitespace, for robust word-for-word comparisons
    (page text extraction may add or drop spaces around links, brackets and tags)."""
    return re.sub(r"\s+", "", normalize_for_match(text))


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[\"“(\[]?[A-Z0-9])")


def verbatim_summary(summary: str, page_text: str) -> tuple[str, int, int]:
    """Keep only the sentences of `summary` that appear word-for-word on the page.

    Returns (verified summary, sentences kept, sentences dropped). Paragraph breaks are
    preserved. This guarantees the digest never shows text the company did not publish.
    """
    page = _compact(page_text)
    kept_paragraphs: list[str] = []
    kept = dropped = 0
    for paragraph in summary.split("\n"):
        sentences = [x.strip() for x in _SENTENCE_RE.split(paragraph.strip()) if x.strip()]
        good: list[str] = []
        for sentence in sentences:
            core = _compact(sentence).rstrip(".!?\"”'’")
            if not core or core in page:
                good.append(sentence)
                kept += 1
            else:
                dropped += 1
        if good:
            kept_paragraphs.append(" ".join(good))
    return "\n\n".join(kept_paragraphs), kept, dropped


def _published_title(listing_title: str, page: ArticlePage) -> str:
    """The headline as published by the company.

    The newsroom listing's headline is kept when it also appears on the article page;
    otherwise the article page's own headline (H1 or og:title) is used when it is clearly
    the same story.
    """
    haystack = _compact("\n".join([page.h1, page.meta_title, page.text[:4000]]))
    if _compact(listing_title) in haystack:
        return listing_title
    listing_words = set(normalize_for_match(listing_title).split())
    best, best_overlap = listing_title, 0.0
    for candidate in (page.h1, page.meta_title):
        words = set(normalize_for_match(candidate).split())
        if not words or not listing_words:
            continue
        overlap = len(words & listing_words) / len(listing_words)
        if overlap > best_overlap:
            best, best_overlap = collapse_ws(candidate), overlap
    return best if best_overlap >= 0.6 else listing_title


def _verified_title(candidate: str, haystack_norm: str, fallback: str) -> str:
    """Return the candidate title if it appears on the page, else a page-sourced fallback."""
    candidate = collapse_ws(candidate)
    if candidate and normalize_for_match(candidate) in haystack_norm:
        return candidate
    fallback = collapse_ws(fallback)
    if fallback and not _GENERIC_LINK_TEXT.match(fallback) and len(fallback.split()) >= 3:
        if candidate:
            logger.debug("Title not found verbatim; using link text. %r -> %r", candidate, fallback)
        return fallback
    return candidate or fallback


async def extract_listing(
    client: OpenAIClient,
    source_name: str,
    source_url: str,
    page_url: str,
    digest: LinkDigest,
    today: date,
) -> tuple[list[ArticleListing], str | None]:
    """Extract article entries from a listing-page digest.

    Returns:
        (articles, listing_url). listing_url is set only when the page holds no articles
        but links to the real listing page.

    Raises:
        OpenAIClientError: If the model call fails.
    """
    user_content = (
        f"Today's date: {today.isoformat()}\n"
        f"Company: {source_name}\n"
        f"Page URL: {page_url}\n"
        f"Page title: {digest.title}\n\n"
        f"Page text:\n{digest.text}"
    )
    data = await client.complete_json(
        _LISTING_SYSTEM_PROMPT, user_content, "newsroom_listing", _LISTING_SCHEMA
    )

    page_norm = normalize_for_match(digest.plain_text)
    articles: list[ArticleListing] = []
    seen_keys: set[str] = set()
    for item in data.get("articles", []):
        link = digest.links.get(item.get("link_id"))
        if link is None:
            logger.debug("%s: model returned unknown link id %r", source_name, item.get("link_id"))
            continue
        key = url_key(link.url)
        if key in seen_keys:
            continue
        title = _strip_trailing_date(_verified_title(item.get("title", ""), page_norm, link.text))
        if not title:
            continue
        seen_keys.add(key)
        articles.append(
            ArticleListing(
                title=title,
                url=link.url,
                date=_clean_date(item.get("date"), today),
                source_name=source_name,
                source_url=source_url,
            )
        )

    listing_url: str | None = None
    if not articles and data.get("listing_link_id") is not None:
        link = digest.links.get(data["listing_link_id"])
        if link is not None:
            listing_url = link.url
    return articles, listing_url


def _lead_paragraphs(text: str, max_words: int = 300) -> str:
    """Deterministic fallback summary: the first substantial paragraphs of the body."""
    picked: list[str] = []
    words = 0
    for line in text.split("\n"):
        count = len(line.split())
        if count < 20:
            continue
        picked.append(line)
        words += count
        if words >= max_words:
            break
    return "\n\n".join(picked)


async def extract_article(
    client: OpenAIClient,
    listing: ArticleListing,
    page: ArticlePage,
    today: date,
) -> dict[str, Any]:
    """Extract headline, date and opening text from a fetched article page.

    Never raises for model errors: falls back to deterministic extraction instead, so an
    article is never lost because of an LLM hiccup.

    Returns:
        Dict with keys title, date, summary (summary may be empty).
    """
    haystack = "\n".join([page.h1, page.meta_title, listing.title, page.text[:4000]])
    haystack_norm = normalize_for_match(haystack)
    meta_date = page.published.isoformat() if page.published else None

    if not page.text.strip():
        return {"title": listing.title, "date": listing.date or meta_date, "summary": ""}

    user_content = (
        f"Article URL: {listing.url}\n"
        f"Headline on the newsroom listing: {listing.title}\n"
        f"Page H1: {page.h1 or '(none)'}\n"
        f"Page meta title: {page.meta_title or '(none)'}\n"
        f"Published date from metadata: {meta_date or '(none)'}\n\n"
        f"Article text:\n{page.text[:12000]}"
    )
    try:
        data = await client.complete_json(
            _ARTICLE_SYSTEM_PROMPT, user_content, "news_article", _ARTICLE_SCHEMA
        )
        summary = (data.get("summary") or "").strip()
    except OpenAIClientError as exc:
        logger.warning("Article extraction fell back to plain text for %s: %s", listing.url, exc)
        data = {}
        # Only when the model is unavailable; an intentionally empty summary (no article
        # body on the page) must not be replaced by whatever text the page has.
        summary = _lead_paragraphs(page.text)

    # Only text the company published may appear in the digest: drop any sentence that is
    # not on the page word-for-word; if much was dropped, copy the opening paragraphs.
    if summary:
        verified, kept, dropped = verbatim_summary(summary, page.text)
        if dropped:
            logger.info(
                "Summary for %s: %d sentence(s) not found word-for-word on the page were removed.",
                listing.url,
                dropped,
            )
        summary = verified if kept and kept >= 2 * dropped else _lead_paragraphs(page.text)

    # Keep the headline exactly as the company published it.
    if listing.title.rstrip().endswith(("...", "…")):
        title = _verified_title(data.get("title", ""), haystack_norm, page.h1 or listing.title)
    else:
        title = _published_title(listing.title, page)
    # The date the newsroom itself displays wins; page metadata and the model are fallbacks.
    article_date = listing.date or meta_date or _clean_date(data.get("date"), today)
    return {"title": title, "date": article_date, "summary": summary}
