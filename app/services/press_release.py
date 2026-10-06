"""
Press-release writer.

Turns a company press release (any article in the digest, or any URL) into an article in
EE Herald's house style, following the editable rules in press_release_guidelines.md.

Accuracy safeguards:
* The model only receives the source text and is instructed to use nothing else.
* After writing, every number and every quotation in the draft is checked against the
  source text; anything that cannot be found is listed in `warnings` for the editor.

Drafts are saved to <OUTPUT_DIR>/press_releases/ as JSON (for tools) and Markdown.
"""

import json
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.exceptions import AppBaseException, FirecrawlClientError
from app.infra.firecrawl import FirecrawlClient
from app.infra.http import HttpFetcher
from app.infra.openai import OpenAIClient
from app.models import PressReleaseDraft
from app.services.parsing import (
    clean_url,
    collapse_ws,
    extract_article_page,
    normalize_for_match,
    url_key,
)
from app.settings import Settings

logger = logging.getLogger(__name__)

_MIN_SOURCE_CHARS = 400
_MAX_SOURCE_CHARS = 30_000

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "section": {"type": "string", "enum": ["news", "new-products"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "paragraphs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "section", "tags", "summary", "paragraphs"],
    "additionalProperties": False,
}

_SYSTEM_TEMPLATE = """You are a senior technology journalist on the EE Herald news desk. You turn company press releases into EE Herald articles.

Follow these house-style guidelines exactly:

<guidelines>
{guidelines}
</guidelines>

Output (JSON):
- "headline": the article headline (rules above).
- "section": "news" or "new-products".
- "tags": 2-4 topic tags.
- "summary": one sentence (max 30 words) summarising the article, for the article teaser / meta description.
- "paragraphs": the article body, one string per paragraph, in order. Plain text only - no markdown, no headings, no bullet characters.

The source press release is the ONLY source of facts. Every number, name, part number, date and quote must come from it."""


class PressReleaseError(AppBaseException):
    """Raised when a press release cannot be generated (e.g. source text unavailable)."""


# ---------------------------------------------------------------------------
# Source text
# ---------------------------------------------------------------------------


def find_digest_article(url: str, output_dir: Path) -> dict[str, Any] | None:
    """Find an article in the saved digest JSON exports (newest first) by URL."""
    key = url_key(url)
    exports = sorted(output_dir.glob("News_Digest_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in exports:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for article in data.get("articles", []):
            if url_key(article.get("url", "")) == key:
                return article
    return None


async def _get_source_text(
    url: str, settings: Settings, article: dict[str, Any] | None
) -> tuple[str, str, str | None, str]:
    """Best available source text. Returns (title, text, published_date, fetched_via).

    Order: live page fetch (free, freshest extraction) -> text saved in the digest ->
    Firecrawl (1 credit, only while the balance is above the reserve).
    """
    async with HttpFetcher(settings) as http:
        result = await http.fetch(url)
    page = None
    if result is not None and result.ok and not result.is_challenge and result.is_html:
        page = extract_article_page(result.content, result.final_url)
        if len(page.text) >= _MIN_SOURCE_CHARS:
            return page.h1 or page.meta_title, page.text, _iso(page.published), "direct"

    if article and len(article.get("body_text") or "") >= _MIN_SOURCE_CHARS:
        return article.get("title") or "", article["body_text"], article.get("published_date"), "digest"

    if settings.firecrawl_enabled and settings.firecrawl_api_key:
        client = FirecrawlClient(settings)
        balance = await client.remaining_credits()
        if balance and balance[0] > settings.firecrawl_credit_reserve:
            try:
                html = await client.scrape_html(url)
                rendered = extract_article_page(html, url)
                if len(rendered.text) >= _MIN_SOURCE_CHARS:
                    return (
                        rendered.h1 or rendered.meta_title,
                        rendered.text,
                        _iso(rendered.published),
                        "firecrawl",
                    )
            except FirecrawlClientError as exc:
                logger.warning("Firecrawl could not render %s: %s", url, exc)

    if page is not None and len(page.text) >= 150:
        return page.h1 or page.meta_title, page.text, _iso(page.published), "direct"
    raise PressReleaseError(
        "Could not read the article text from the source page (blocked or rendered by "
        "JavaScript, and no Firecrawl credit was available)."
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Fact check
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*")
_QUOTE_RE = re.compile(r"[“\"]([^“”\"]{25,})[”\"]")


def _normalize_numbers(text: str) -> str:
    # 20,000 -> 20000 so thousands separators don't cause false alarms
    return re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)


def fact_check(paragraphs: list[str], headline: str, source_text: str) -> list[str]:
    """List figures and quotations in the draft that do not appear in the source."""
    source_numbers = _normalize_numbers(source_text)
    source_norm = normalize_for_match(source_text)
    draft = "\n".join([headline, *paragraphs])
    warnings: list[str] = []

    missing_numbers: list[str] = []
    for match in _NUMBER_RE.finditer(_normalize_numbers(draft)):
        number = match.group(0).rstrip(".,")
        pattern = rf"(?<![\d.]){re.escape(number)}(?![\d])"
        if not re.search(pattern, source_numbers) and number not in missing_numbers:
            missing_numbers.append(number)
    if missing_numbers:
        warnings.append(
            "Figures not found in the source - please verify: " + ", ".join(missing_numbers)
        )

    for match in _QUOTE_RE.finditer(draft):
        quote = collapse_ws(match.group(1))
        # Check sentence by sentence: a quote the source split around "said X" may be
        # correctly merged into one, but each sentence must still be verbatim.
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", quote) if len(s.split()) >= 5]
        for sentence in sentences or [quote]:
            if normalize_for_match(sentence.rstrip(".,!?")) not in source_norm:
                warnings.append(
                    f"Quotation not found word-for-word in the source: “{sentence[:140]}”"
                )
    return warnings


def display_date(value: datetime) -> str:
    """EE Herald date style: October 7, 2026."""
    return f"{value:%B} {value.day}, {value.year}"


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _load_guidelines(settings: Settings) -> str:
    path = Path(settings.press_release_guidelines_file)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PressReleaseError(f"Guidelines file not found: {path} ({exc})") from exc


def _slug(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "article"


async def generate_press_release(
    url: str,
    settings: Settings,
    source_title: str | None = None,
    source_name: str | None = None,
) -> PressReleaseDraft:
    """Write an EE Herald-style article from the press release at `url` and save it.

    Raises:
        PressReleaseError: If the source text cannot be obtained or the model fails.
    """
    url = clean_url(url)
    output_dir = Path(settings.output_dir)
    article = find_digest_article(url, output_dir)

    fetched_title, text, published, fetched_via = await _get_source_text(url, settings, article)
    if article:
        source_title = source_title or article.get("title")
        source_name = source_name or article.get("source_name")
        published = article.get("published_date") or published
    source_title = source_title or fetched_title

    guidelines = _load_guidelines(settings)
    llm = OpenAIClient(
        settings,
        model=settings.press_release_model or None,
        reasoning_effort=settings.press_release_reasoning_effort,
    )
    user_content = (
        f"Source press release\n"
        f"Company / newsroom: {source_name or 'unknown'}\n"
        f"Title: {source_title or 'unknown'}\n"
        f"Published: {published or 'unknown'}\n"
        f"URL: {url}\n"
        f"Today's date: {datetime.now():%Y-%m-%d}\n\n"
        f"Text:\n{text[:_MAX_SOURCE_CHARS]}"
    )
    try:
        data = await llm.complete_json(
            _SYSTEM_TEMPLATE.format(guidelines=guidelines),
            user_content,
            "eeherald_article",
            _SCHEMA,
        )
    except AppBaseException as exc:
        raise PressReleaseError(f"Article generation failed: {exc.message}") from exc

    paragraphs = [collapse_ws(p) for p in data.get("paragraphs", []) if collapse_ws(p)]
    headline = collapse_ws(data.get("headline", "")).rstrip(".")
    if not headline or not paragraphs:
        raise PressReleaseError("The model returned an empty article. Please try again.")

    words = sum(len(p.split()) for p in paragraphs)
    now = datetime.now().replace(microsecond=0)
    draft = PressReleaseDraft(
        id=f"{now:%Y%m%d_%H%M%S}_{_slug(headline, 50)}",
        created_at=now,
        headline=headline,
        section=data.get("section", "news"),
        tags=[collapse_ws(t) for t in data.get("tags", []) if collapse_ws(t)][:4],
        summary=collapse_ws(data.get("summary", "")),
        paragraphs=paragraphs,
        byline=settings.press_release_byline,
        word_count=words,
        read_minutes=max(1, math.ceil(words / 200)),
        source_title=source_title or "",
        source_url=url,
        source_name=source_name,
        source_published_date=published,
        source_fetched_via=fetched_via,
        model=llm.model,
        openai_cost_usd=round(llm.usage.cost_usd, 6),
        warnings=fact_check(paragraphs, headline, text),
    )
    save_draft(draft, output_dir)
    logger.info(
        "Press release written: %r (%d words, $%.4f, %d warnings)",
        draft.headline,
        words,
        draft.openai_cost_usd,
        len(draft.warnings),
    )
    return draft


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def drafts_dir(output_dir: Path) -> Path:
    return output_dir / "press_releases"


def to_markdown(draft: PressReleaseDraft) -> str:
    section = "New Products" if draft.section == "new-products" else "News"
    lines = [
        f"# {draft.headline}",
        "",
        f"*By {draft.byline} | {display_date(draft.created_at)} | {draft.read_minutes} min read*",
        "",
        f"Section: {section}" + (f" | Tags: {', '.join(draft.tags)}" if draft.tags else ""),
        "",
    ]
    for paragraph in draft.paragraphs:
        lines += [paragraph, ""]
    lines += ["---", f"Source: [{draft.source_title or draft.source_url}]({draft.source_url})"]
    if draft.warnings:
        lines += ["", "Check before publishing:"] + [f"- {w}" for w in draft.warnings]
    return "\n".join(lines) + "\n"


def save_draft(draft: PressReleaseDraft, output_dir: Path) -> None:
    folder = drafts_dir(output_dir)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{draft.id}.json").write_text(draft.model_dump_json(indent=2), encoding="utf-8")
        (folder / f"{draft.id}.md").write_text(to_markdown(draft), encoding="utf-8")
    except OSError as exc:
        logger.error("Could not save press release %s: %s", draft.id, exc)


def list_drafts(output_dir: Path) -> list[PressReleaseDraft]:
    drafts = []
    for path in sorted(drafts_dir(output_dir).glob("*.json"), reverse=True):
        try:
            drafts.append(PressReleaseDraft.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return drafts


def load_draft(draft_id: str, output_dir: Path) -> PressReleaseDraft | None:
    if not re.fullmatch(r"[\w\-]+", draft_id):
        return None
    path = drafts_dir(output_dir) / f"{draft_id}.json"
    try:
        return PressReleaseDraft.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def find_draft_for_url(url: str, output_dir: Path) -> PressReleaseDraft | None:
    key = url_key(url)
    for draft in list_drafts(output_dir):
        if url_key(draft.source_url) == key:
            return draft
    return None
