"""
Press-release writer.

Turns a company press release (any article in the digest, or any URL) into an article in
EE Herald's house style, following the editable rules in press_release_guidelines.md.

Accuracy safeguards:
* The model only receives the source text and is instructed to use nothing else.
* After writing, every number and every quotation in the draft is checked against the
  source text; anything that cannot be found is listed in `warnings` for the editor.

Drafts are saved to <OUTPUT_DIR>/press_releases/ as JSON (for tools) and Markdown; the
article's images are saved in <OUTPUT_DIR>/press_releases/<draft id>/.
"""

import asyncio
import json
import logging
import math
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.exceptions import AppBaseException, FirecrawlClientError
from app.infra.firecrawl import FirecrawlClient
from app.infra.http import HttpFetcher
from app.infra.openai import OpenAIClient
from app.models import DraftImage, PressReleaseDraft
from app.services.images import DownloadedImage, describe_alt, download_images, make_featured
from app.services.parsing import (
    ImageRef,
    clean_url,
    collapse_ws,
    extract_article_page,
    good_image,
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

The source press release is the ONLY source of information. Every fact, number, name, part number, date, quote, explanation and piece of context must come from it. Do not use general knowledge. If something is not in the source, leave it out - a shorter article is better than one with added information."""


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
) -> tuple[str, str, str | None, str, list[ImageRef]]:
    """Best available source text and images.

    Returns (title, text, published_date, fetched_via, image_refs).
    Order: live page fetch (free, freshest extraction) -> text saved in the digest ->
    Firecrawl (1 credit, only while the balance is above the reserve).
    """
    digest_images = _news_image(article)
    async with HttpFetcher(settings) as http:
        result = await http.fetch(url)
    page = None
    if result is not None and result.ok and not result.is_challenge and result.is_html:
        page = extract_article_page(result.content, result.final_url)
        if len(page.text) >= _MIN_SOURCE_CHARS:
            return (
                page.h1 or page.meta_title,
                page.text,
                _iso(page.published),
                "direct",
                _merge_refs(digest_images, page.images),
            )

    if article and len(article.get("body_text") or "") >= _MIN_SOURCE_CHARS:
        images = _merge_refs(digest_images, page.images if page else [])
        return (
            article.get("title") or "",
            article["body_text"],
            article.get("published_date"),
            "digest",
            images,
        )

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
                        _merge_refs(digest_images, rendered.images),
                    )
            except FirecrawlClientError as exc:
                logger.warning("Firecrawl could not render %s: %s", url, exc)

    if page is not None and len(page.text) >= 150:
        return (
            page.h1 or page.meta_title,
            page.text,
            _iso(page.published),
            "direct",
            _merge_refs(digest_images, page.images),
        )
    raise PressReleaseError(
        "Could not read the article text from the source page (blocked or rendered by "
        "JavaScript, and no Firecrawl credit was available)."
    )


def _news_image(article: dict[str, Any] | None) -> list[ImageRef]:
    """The image extracted with the news item in the digest (if any)."""
    if article and article.get("image_url") and good_image(article["image_url"]):
        return [ImageRef(url=article["image_url"], origin="news")]
    return []


def _merge_refs(first: list[ImageRef], rest: list[ImageRef]) -> list[ImageRef]:
    seen = {r.url for r in first}
    return first + [r for r in rest if r.url not in seen]


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

    unsourced = unsourced_terms(paragraphs, headline, source_text)
    if unsourced:
        warnings.append(
            "Names or terms not found in the source - please verify: " + ", ".join(unsourced)
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


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9&+./\-]*[A-Za-z0-9]|[A-Za-z]")
_MONTHS_AND_DAYS = {
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug",
    "sep", "sept", "oct", "nov", "dec", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
}


def _squash(text: str) -> str:
    return re.sub(r"[\s\-]+", "", normalize_for_match(text))


def unsourced_terms(paragraphs: list[str], headline: str, source_text: str) -> list[str]:
    """Proper names, acronyms and part numbers in the draft that the source never mentions.

    Checks: words containing digits (part numbers, "5G"), all-caps acronyms, and
    capitalised words that do not start a sentence (names of people, companies, places,
    products). Comparison ignores case, spaces and hyphens; a plural "s" is tolerated.
    """
    source = _squash(source_text)

    def in_source(term: str) -> bool:
        squashed = _squash(term).strip("./")
        return not squashed or squashed in source or (
            squashed.endswith("s") and squashed[:-1] in source
        )

    found: list[str] = []

    def consider(term: str) -> None:
        term = term.strip("./-")
        if len(term) < 2 or term.lower() in _MONTHS_AND_DAYS or term in found:
            return
        if not in_source(term):
            found.append(term)

    # Headline: only part numbers and acronyms (it is written in Title Case).
    for match in _WORD_RE.finditer(headline):
        word = match.group(0)
        if any(c.isdigit() for c in word) or (word.isupper() and len(word) >= 2):
            consider(word)

    for paragraph in paragraphs:
        for match in _WORD_RE.finditer(paragraph):
            word = match.group(0)
            before = paragraph[: match.start()].rstrip()
            sentence_start = not before or before[-1] in '.!?:;"“(‘\''
            if any(c.isdigit() for c in word) or (word.isupper() and len(word) >= 2):
                consider(word)
            elif word[0].isupper() and not sentence_start:
                consider(word)
    return found


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

    fetched_title, text, published, fetched_via, image_refs = await _get_source_text(
        url, settings, article
    )
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
    # Write the article and download the source's images at the same time.
    try:
        data, (downloaded, failed_images) = await asyncio.gather(
            llm.complete_json(
                _SYSTEM_TEMPLATE.format(guidelines=guidelines),
                user_content,
                "eeherald_article",
                _SCHEMA,
            ),
            _download_source_images(image_refs, url, settings),
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
        image_after_paragraph=min(2, len(paragraphs)),
    )
    _store_images(draft, downloaded, failed_images, image_refs, output_dir)
    save_draft(draft, output_dir)
    logger.info(
        "Press release written: %r (%d words, %d images, $%.4f, %d warnings)",
        draft.headline,
        words,
        len(draft.images),
        draft.openai_cost_usd,
        len(draft.warnings),
    )
    return draft


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

FEATURED_FILE = "featured-1200x800.png"
NO_IMAGE_NOTE = "No image found in the source article."


async def _download_source_images(
    refs: list[ImageRef], referer: str, settings: Settings
) -> tuple[list[DownloadedImage], list[str]]:
    """Download usable images; never fails the article (returns no images on any error)."""
    if not refs:
        return [], []
    try:
        async with HttpFetcher(settings) as http:
            return await download_images(refs, referer, http)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Image download failed for %s: %s", referer, exc)
        return [], [r.url for r in refs]


def image_dir(output_dir: Path, draft_id: str) -> Path:
    return drafts_dir(output_dir) / draft_id


def _image_note(refs: list[ImageRef], failed: list[str]) -> str:
    """Explain why an article has no image."""
    if not refs:
        return NO_IMAGE_NOTE
    news = [r.url for r in refs if r.origin == "news"]
    tried = news[0] if news else (failed[0] if failed else refs[0].url)
    return (
        "An image was found in the source article but could not be downloaded or was not "
        f"usable (e.g. a logo or icon): {tried}"
    )


def _store_images(
    draft: PressReleaseDraft,
    downloaded: list[DownloadedImage],
    failed: list[str],
    refs: list[ImageRef],
    output_dir: Path,
) -> None:
    """Save the original image files next to the draft and use the first one."""
    folder = image_dir(output_dir, draft.id)
    shutil.rmtree(folder, ignore_errors=True)
    draft.images, draft.featured_index, draft.featured_file, draft.image_alt = [], None, None, ""
    draft.image_note = ""
    if not downloaded:
        draft.image_note = _image_note(refs, failed)
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for number, image in enumerate(downloaded, start=1):
            name = f"image-{number}.{image.extension}"
            (folder / name).write_bytes(image.data)  # original bytes, unmodified
            draft.images.append(
                DraftImage(
                    file=name,
                    source_url=image.source_url,
                    alt=image.alt,
                    origin=image.origin,
                    width=image.width,
                    height=image.height,
                )
            )
    except OSError as exc:
        logger.error("Could not save images for %s: %s", draft.id, exc)
        draft.images = []
        draft.image_note = f"The image could not be saved: {exc}"
        return
    set_featured_image(draft, 0, output_dir)


def set_featured_image(draft: PressReleaseDraft, index: int | None, output_dir: Path) -> None:
    """Use image `index` (EE Herald 1200x800 copy of it), or no image when None."""
    folder = image_dir(output_dir, draft.id)
    (folder / FEATURED_FILE).unlink(missing_ok=True)
    if index is None or not (0 <= index < len(draft.images)):
        draft.featured_index, draft.featured_file, draft.image_alt = None, None, ""
        draft.image_note = (
            "No image selected by the editor." if draft.images else draft.image_note or NO_IMAGE_NOTE
        )
        return
    image = draft.images[index]
    try:
        (folder / FEATURED_FILE).write_bytes(make_featured((folder / image.file).read_bytes()))
    except (OSError, ValueError) as exc:
        logger.error("Could not prepare the article image for %s: %s", draft.id, exc)
        draft.featured_index, draft.featured_file = None, None
        draft.image_note = f"The image could not be prepared: {exc}"
        return
    draft.featured_index = index
    draft.featured_file = FEATURED_FILE
    draft.image_alt = describe_alt(image.alt, draft.headline)
    draft.image_note = ""


async def refresh_images(draft: PressReleaseDraft, settings: Settings) -> PressReleaseDraft:
    """Re-collect images for an existing draft from its source page (text unchanged)."""
    output_dir = Path(settings.output_dir)
    refs: list[ImageRef] = []
    async with HttpFetcher(settings) as http:
        result = await http.fetch(draft.source_url)
    if result is not None and result.ok and not result.is_challenge and result.is_html:
        refs = extract_article_page(result.content, result.final_url).images
    refs = _merge_refs(_news_image(find_digest_article(draft.source_url, output_dir)), refs)
    downloaded, failed = await _download_source_images(refs, draft.source_url, settings)
    _store_images(draft, downloaded, failed, refs, output_dir)
    draft.image_after_paragraph = min(2, len(draft.paragraphs))
    save_draft(draft, output_dir)
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
    for number, paragraph in enumerate(draft.paragraphs, start=1):
        lines += [paragraph, ""]
        if number == draft.image_after_paragraph:
            if draft.featured_file:
                lines += [f"![{draft.image_alt}]({draft.id}/{draft.featured_file})", ""]
            elif draft.image_note:
                lines += [f"*[Image: {draft.image_note}]*", ""]
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
