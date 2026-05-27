"""
Main pipeline orchestrator and Word document builder.

This module is the primary entrypoint called by both the FastAPI /run
endpoint and the standalone app/run.py script.
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    Image as RLImage,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
)
from reportlab.platypus.tableofcontents import TableOfContents

from app.exceptions import DocumentBuildError, StatePersistenceError
from app.infra.firecrawl import FirecrawlClient
from app.infra.openai import OpenAIClient
from app.models import DigestRunResult
from app.services.agent import extract_article_contents, extract_articles_from_listings
from app.services.crawling import scrape_articles, scrape_listing_pages
from app.settings import Settings
from app.sites import SITES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def load_state(state_path: Path) -> dict[str, Any]:
    """Load run state from the JSON state file.

    Args:
        state_path: Path to scraper_state.json.

    Returns:
        State dict with at minimum a 'seen_urls' key.
        Returns empty state if the file does not exist.

    Raises:
        StatePersistenceError: If the file exists but cannot be read or parsed.
    """
    if not state_path.exists():
        logger.info("No state file at %s. Starting with empty state.", state_path)
        return {"seen_urls": {}}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise StatePersistenceError(
            f"Failed to read state file {state_path}: {exc}"
        ) from exc


def save_state(
    state_path: Path,
    seen_urls: dict[str, str],
    run_date: str,
) -> None:
    """Persist run state to the JSON state file.

    Args:
        state_path: Path to scraper_state.json.
        seen_urls: Mapping of article URL to the ISO date it was first seen.
        run_date: ISO date string for the current run.

    Raises:
        StatePersistenceError: If the file cannot be written.
    """
    state = {"last_run_date": run_date, "seen_urls": seen_urls}
    try:
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        logger.info("State saved to %s. Tracking %d URLs.", state_path, len(seen_urls))
    except OSError as exc:
        raise StatePersistenceError(
            f"Failed to write state file {state_path}: {exc}"
        ) from exc


def _is_within_lookback(date_str: str | None, lookback_days: int) -> bool:
    """Check if a date string is within the lookback window.

    Args:
        date_str: Date string (YYYY-MM-DD format preferred; other formats attempted).
        lookback_days: Number of days to look back from today.

    Returns:
        True if the date is within the lookback window, False otherwise.
        Returns True if the date cannot be parsed or is null (benefit of the doubt).
    """
    if not date_str:
        # Cannot determine age — include the article to avoid silently dropping new content
        return True
    try:
        # Try ISO format first
        article_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        try:
            # Try common alternative formats
            for fmt in ["%B %d, %Y", "%b %d, %Y", "%d-%m-%Y", "%m/%d/%Y"]:
                try:
                    article_date = datetime.strptime(date_str, fmt).date()
                    break
                except ValueError:
                    continue
            else:
                logger.debug("Could not parse date: %s", date_str)
                return False
        except Exception:
            return False

    cutoff = date.today() - timedelta(days=lookback_days)
    return article_date >= cutoff


# ---------------------------------------------------------------------------
# Document building helpers
# ---------------------------------------------------------------------------

_PAGE_W, _PAGE_H = A4
_MARGIN = 0.75 * inch

# Use Arial (Unicode) when available on Windows; fall back to built-in Helvetica.
_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"
_FONT_ITALIC = "Helvetica-Oblique"
try:
    pdfmetrics.registerFont(TTFont("_NxpArial", "C:/Windows/Fonts/Arial.ttf"))
    pdfmetrics.registerFont(TTFont("_NxpArial-Bold", "C:/Windows/Fonts/Arialbd.ttf"))
    pdfmetrics.registerFont(TTFont("_NxpArial-Italic", "C:/Windows/Fonts/Ariali.ttf"))
    _FONT = "_NxpArial"
    _FONT_BOLD = "_NxpArial-Bold"
    _FONT_ITALIC = "_NxpArial-Italic"
except Exception:
    pass  # Helvetica Latin-1 fallback

# Map common Unicode characters that Helvetica cannot render to ASCII equivalents.
_UNICODE_MAP = str.maketrans({
    "\u2019": "'",  "\u2018": "'",
    "\u201c": '"',  "\u201d": '"',
    "\u2013": "-",  "\u2014": "--",
    "\u2022": "*",  "\u2026": "...",
    "\u00ae": "(R)","\u2122": "(TM)",
    "\u00a0": " ",  "\u00ad": "",
    "\u2011": "-",  "\u2010": "-",
    "\u00b7": ".",
})


def _clean(text: str) -> str:
    """Replace problematic Unicode characters and XML-escape for reportlab markup."""
    return (
        text.translate(_UNICODE_MAP)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class _DigestTemplate(BaseDocTemplate):
    """BaseDocTemplate subclass that wires source headings into the TOC."""

    def afterFlowable(self, flowable: Any) -> None:
        """Bookmark heading paragraphs and emit TOC entry notifications.

        Handles both bare Paragraphs and Paragraphs nested inside a
        KeepTogether block (where the heading is bundled with the first
        article to prevent orphaned headings at the bottom of a page).
        """
        candidates = (
            flowable._flowables
            if isinstance(flowable, KeepTogether)
            else [flowable]
        )
        for f in candidates:
            if isinstance(f, Paragraph):
                key = getattr(f, "_bookmark_key", None)
                if key:
                    self.canv.bookmarkPage(key)
                    self.notify("TOCEntry", (0, f.getPlainText(), self.page, key))


def _draw_page_footer(canvas: Any, doc: Any) -> None:
    """Draw a centred page number in the footer of every page."""
    canvas.saveState()
    canvas.setFont(_FONT, 9)
    canvas.setFillColor(colors.HexColor("#999999"))
    canvas.drawCentredString(
        doc.leftMargin + doc.width / 2,
        doc.bottomMargin - 0.35 * inch,
        str(canvas.getPageNumber()),
    )
    canvas.restoreState()


def _get_styles() -> dict[str, ParagraphStyle]:
    """Build and return paragraph styles for the PDF digest."""
    base = getSampleStyleSheet()
    return {
        "cover_title": ParagraphStyle(
            "cover_title",
            parent=base["Title"],
            fontName=_FONT_BOLD,
            fontSize=28,
            leading=34,
            textColor=colors.HexColor("#1a1a2e"),
            alignment=TA_CENTER,
            spaceAfter=12,
        ),
        "cover_sub": ParagraphStyle(
            "cover_sub",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=13,
            leading=18,
            textColor=colors.HexColor("#444444"),
            alignment=TA_CENTER,
            spaceAfter=6,
        ),
        "toc_title": ParagraphStyle(
            "toc_title",
            parent=base["Normal"],
            fontName=_FONT_BOLD,
            fontSize=14,
            leading=18,
            textColor=colors.HexColor("#1a1a2e"),
            spaceBefore=12,
            spaceAfter=6,
        ),
        "toc_entry": ParagraphStyle(
            "toc_entry",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=11,
            leading=18,
            leftIndent=0,
            rightIndent=36,
            spaceBefore=2,
            textColor=colors.HexColor("#0057b7"),
        ),
        "source_heading": ParagraphStyle(
            "source_heading",
            parent=base["Heading1"],
            fontName=_FONT_BOLD,
            fontSize=15,
            leading=20,
            textColor=colors.HexColor("#1a1a2e"),
            spaceBefore=20,
            spaceAfter=4,
        ),
        "article_title": ParagraphStyle(
            "article_title",
            parent=base["Normal"],
            fontName=_FONT_BOLD,
            fontSize=11,
            leading=16,
            textColor=colors.HexColor("#0057b7"),
            spaceBefore=10,
            spaceAfter=2,
        ),
        "article_date": ParagraphStyle(
            "article_date",
            parent=base["Normal"],
            fontName=_FONT_ITALIC,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#888888"),
            spaceAfter=4,
        ),
        "article_body": ParagraphStyle(
            "article_body",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=10,
            leading=15,
            textColor=colors.HexColor("#222222"),
            spaceAfter=6,
            alignment=TA_JUSTIFY,
        ),
    }


def _fetch_image(image_url: str, max_width: float = 5.0 * inch) -> RLImage | None:
    """Download an image and return a reportlab Image flowable.

    Returns None on any download or format failure so the article is
    still rendered without an image.

    Args:
        image_url: URL of the image to download.
        max_width: Maximum rendered width in points.
    """
    try:
        response = httpx.get(image_url, timeout=10, follow_redirects=True)
        response.raise_for_status()
        data = BytesIO(response.content)
        pil_img = PILImage.open(data)
        w, h = pil_img.size
        aspect = h / w
        rendered_w = min(max_width, _PAGE_W - 2 * _MARGIN)
        rendered_h = rendered_w * aspect
        data.seek(0)
        return RLImage(data, width=rendered_w, height=rendered_h)
    except Exception as exc:
        logger.warning("Failed to embed image from %s: %s", image_url, exc)
        return None


def _build_document(
    articles_by_source: dict[str, list[dict[str, Any]]],
    output_dir: Path,
    run_date: date,
) -> Path:
    """Build the PDF news digest.

    Features:
    - First page: cover + clickable table of contents with page numbers.
    - Each article block is kept together on one page where it fits.
    - Article titles are clickable hyperlinks to the source URL.
    - Body text is justified.
    - Page numbers in the footer of every page.

    Args:
        articles_by_source: Articles grouped by source/company name.
        output_dir: Directory where the .pdf file will be saved.
        run_date: Date of this pipeline run; used in cover page and filename.

    Returns:
        Path to the saved .pdf file.

    Raises:
        DocumentBuildError: If the document cannot be saved.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"News_Digest_{run_date.strftime('%d_%m_%Y')}.pdf"

    styles = _get_styles()
    total = sum(len(v) for v in articles_by_source.values())
    story: list[Any] = []

    # --- Cover + TOC on first page ---
    story.append(Spacer(1, 1.2 * inch))
    story.append(Paragraph("DAILY TECH NEWS DIGEST", styles["cover_title"]))
    story.append(Paragraph(run_date.strftime("%A, %B %d, %Y"), styles["cover_sub"]))
    story.append(
        Paragraph(
            f"{total} new articles across {len(articles_by_source)} sources",
            styles["cover_sub"],
        )
    )
    story.append(Spacer(1, 0.4 * inch))
    story.append(Paragraph("Contents", styles["toc_title"]))
    story.append(Spacer(1, 0.1 * inch))
    toc = TableOfContents()
    toc.levelStyles = [styles["toc_entry"]]
    story.append(toc)
    story.append(PageBreak())

    # --- Articles grouped by source ---
    for source_name, articles in articles_by_source.items():
        # Unique bookmark key for this source section
        key = "src_" + source_name.lower().replace(" ", "_").replace("/", "_").replace(".", "_")

        heading_para = Paragraph(
            _clean(f"{source_name}  ({len(articles)} articles)"),
            styles["source_heading"],
        )
        heading_para._bookmark_key = key  # type: ignore[attr-defined]
        hr = HRFlowable(width="100%", thickness=1, color=colors.HexColor("#dddddd"), spaceAfter=4)

        for i, article in enumerate(articles):
            url = article.get("url", "")
            title = _clean(article.get("title", "Untitled"))
            title_html = (
                f'<link href="{url}" color="#0057b7"><b>{title}</b></link>'
                if url
                else f"<b>{title}</b>"
            )

            # For the first article, lead with the source heading + rule so the
            # heading is never left stranded at the bottom of a page without content.
            block: list[Any] = ([heading_para, hr] if i == 0 else [])
            block.append(Paragraph(title_html, styles["article_title"]))

            if article.get("date"):
                block.append(Paragraph(_clean(article["date"]), styles["article_date"]))

            if article.get("image_url"):
                img = _fetch_image(article["image_url"])
                if img:
                    block.append(img)
                    block.append(Spacer(1, 4))

            if article.get("content"):
                block.append(Paragraph(_clean(article["content"]), styles["article_body"]))

            block.append(Spacer(1, 10))
            story.append(KeepTogether(block))

        story.append(Spacer(1, 6))

    try:
        doc = _DigestTemplate(
            str(output_path),
            pagesize=A4,
            leftMargin=_MARGIN,
            rightMargin=_MARGIN,
            topMargin=_MARGIN,
            bottomMargin=_MARGIN + 0.3 * inch,
            title="Daily Tech News Digest",
            author="NXP Agent",
        )
        frame = Frame(
            doc.leftMargin,
            doc.bottomMargin,
            doc.width,
            doc.height,
            id="normal",
        )
        doc.addPageTemplates([PageTemplate(id="All", frames=[frame], onPage=_draw_page_footer)])
        # multiBuild is required: first pass collects page numbers, second pass fills TOC
        doc.multiBuild(story)
        logger.info("Document saved: %s", output_path)
    except Exception as exc:
        raise DocumentBuildError(f"Failed to save document {output_path}: {exc}") from exc

    return output_path


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------


async def run_pipeline(settings: Settings) -> DigestRunResult:
    """Execute the full news scraping and digest generation pipeline.

    Stages:
        1. Load state (seen URLs).
        2. Scrape all listing pages concurrently via Firecrawl.
        2b. Re-scrape redirect URLs for hub pages that returned no articles.
        3. Filter out URLs already present in seen_urls.
        4. Scrape each new article page concurrently via Firecrawl.
        5. Extract 300-word content and image per article via GPT.
        6. Build Word document grouped by source.
        7. Save updated state (only on reaching this point).

    Args:
        settings: Application settings instance.

    Returns:
        DigestRunResult summarising the run.
    """
    run_date = date.today()
    errors: list[str] = []
    state_path = Path(settings.state_file)

    state = load_state(state_path)
    seen_urls: dict[str, str] = state.get("seen_urls", {})
    last_run_date = state.get("last_run_date")
    is_first_run = last_run_date is None

    firecrawl = FirecrawlClient(settings)
    openai = OpenAIClient(settings)
    firecrawl_semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
    openai_semaphore = asyncio.Semaphore(settings.max_concurrent_openai)

    # Stage 1: Scrape listing pages
    logger.info("Stage 1: Scraping %d listing pages.", len(SITES))
    listing_results = await scrape_listing_pages(firecrawl, SITES, firecrawl_semaphore)
    logger.info(
        "Stage 1 complete. Scraped %d/%d listing pages.", len(listing_results), len(SITES)
    )

    # Stage 2: GPT extract article lists from each listing
    logger.info("Stage 2: Extracting article lists from listing pages.")
    all_articles, redirect_hints = await extract_articles_from_listings(
        openai, listing_results, openai_semaphore
    )
    logger.info("Stage 2 complete. Found %d articles across all sources.", len(all_articles))

    # Stage 2b: Re-scrape sites that returned a redirect URL (hub/nav pages)
    if redirect_hints:
        logger.info(
            "Stage 2b: Following redirect hints for %d sites.", len(redirect_hints)
        )
        redirect_sites = [
            {"name": r["source_name"], "url": r["redirect_url"]} for r in redirect_hints
        ]
        redirect_listings = await scrape_listing_pages(firecrawl, redirect_sites, firecrawl_semaphore)
        redirect_articles, _ = await extract_articles_from_listings(
            openai, redirect_listings, openai_semaphore
        )
        logger.info(
            "Stage 2b complete. Found %d additional articles from redirected pages.",
            len(redirect_articles),
        )
        all_articles.extend(redirect_articles)

    # Stage 3: Filter seen URLs and invalid URLs
    new_articles = [
        a
        for a in all_articles
        if a.get("url")
        and a["url"].startswith("http")
        and a["url"] not in seen_urls
    ]
    logger.info(
        "Stage 3 complete. %d new articles after filtering %d seen URLs.",
        len(new_articles),
        len(seen_urls),
    )

    if not new_articles:
        logger.info("No new articles found. Saving state.")
        today_str = run_date.isoformat()
        save_state(state_path, seen_urls, today_str)
        return DigestRunResult(
            run_date=run_date,
            total_articles=0,
            sources_processed=len(listing_results),
            document_path=None,
            errors=[],
        )

    # First run: populate state with discovered URLs and exit — no doc built.
    # Next run will treat anything new since today as genuinely new articles.
    if is_first_run:
        logger.info(
            "First run: saving %d article URLs to state. Run again tomorrow to get the digest.",
            len(new_articles),
        )
        today_str = run_date.isoformat()
        for article in new_articles:
            seen_urls[article["url"]] = today_str
        save_state(state_path, seen_urls, today_str)
        return DigestRunResult(
            run_date=run_date,
            total_articles=len(new_articles),
            sources_processed=len(listing_results),
            document_path=None,
            errors=errors,
        )

    # Stage 4: Scrape each new article page
    logger.info("Stage 4: Scraping %d new article pages.", len(new_articles))
    scraped_articles = await scrape_articles(firecrawl, new_articles, firecrawl_semaphore)
    logger.info("Stage 4 complete. Scraped %d articles.", len(scraped_articles))

    # Stage 5: GPT extract content from each article
    logger.info("Stage 5: Extracting content from %d articles.", len(scraped_articles))
    extracted_articles = await extract_article_contents(openai, scraped_articles, openai_semaphore)
    logger.info(
        "Stage 5 complete. Extracted content for %d articles.", len(extracted_articles)
    )

    # Stage 6: Build Word document
    logger.info("Stage 6: Building Word document.")
    articles_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for article in extracted_articles:
        articles_by_source[article["source_name"]].append(article)

    doc_path: Path | None = None
    try:
        doc_path = _build_document(
            dict(articles_by_source), Path(settings.output_dir), run_date
        )
    except DocumentBuildError as exc:
        logger.error("Document build failed: %s", exc)
        errors.append(str(exc))

    # Stage 7: Save state
    today_str = run_date.isoformat()
    for article in extracted_articles:
        seen_urls[article["url"]] = today_str
    save_state(state_path, seen_urls, today_str)

    return DigestRunResult(
        run_date=run_date,
        total_articles=len(extracted_articles),
        sources_processed=len(listing_results),
        document_path=str(doc_path) if doc_path else None,
        errors=errors,
    )
