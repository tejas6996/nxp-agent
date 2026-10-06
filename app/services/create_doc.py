"""
Digest document builders: the PDF news digest and its machine-readable JSON twin.

The JSON export carries the full cleaned article text so downstream tools (for example a
future agent that drafts press releases) can consume each run's articles directly.
"""

import json
import logging
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

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
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
)
from reportlab.platypus import (
    Image as RLImage,
)
from reportlab.platypus.tableofcontents import TableOfContents

from app.exceptions import DocumentBuildError
from app.models import ArticleContent, DigestRunResult

logger = logging.getLogger(__name__)

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

# Images narrower/shorter than this are icons, logos or avatars, not article images.
_MIN_IMAGE_W = 200
_MIN_IMAGE_H = 100
_MAX_IMAGE_PIXELS_W = 1200


def _clean(text: str) -> str:
    """Replace problematic Unicode characters and XML-escape for reportlab markup."""
    return (
        text.translate(_UNICODE_MAP)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_attr(value: str) -> str:
    """Escape a value for use inside a reportlab markup attribute (e.g. a link href)."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
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
            getattr(flowable, "_content", [])
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


def prepare_image(data: bytes) -> bytes | None:
    """Validate downloaded image bytes and re-encode them as a compact JPEG.

    Returns None for anything that is not a usable article image (icons, tracking pixels,
    unreadable formats), so the article is still rendered without an image.
    """
    try:
        with PILImage.open(BytesIO(data)) as img:
            img.load()
            width, height = img.size
            if width < _MIN_IMAGE_W or height < _MIN_IMAGE_H:
                return None
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                background = PILImage.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                converted = background
            else:
                converted = img.convert("RGB")
            if width > _MAX_IMAGE_PIXELS_W:
                converted = converted.resize(
                    (_MAX_IMAGE_PIXELS_W, round(height * _MAX_IMAGE_PIXELS_W / width))
                )
            out = BytesIO()
            converted.save(out, format="JPEG", quality=82, optimize=True)
            return out.getvalue()
    except Exception:
        return None


def _image_flowable(data: bytes, max_width: float = 5.0 * inch) -> RLImage | None:
    try:
        with PILImage.open(BytesIO(data)) as img:
            w, h = img.size
        rendered_w = min(max_width, _PAGE_W - 2 * _MARGIN)
        rendered_h = rendered_w * h / w
        max_h = 4.0 * inch
        if rendered_h > max_h:
            rendered_w, rendered_h = rendered_w * max_h / rendered_h, max_h
        return RLImage(BytesIO(data), width=rendered_w, height=rendered_h)
    except Exception as exc:
        logger.warning("Failed to embed image: %s", exc)
        return None


def output_paths(output_dir: Path, run_at: datetime) -> tuple[Path, Path]:
    """Return unique (pdf_path, json_path) for this run.

    The time is part of the name so several runs on the same day never overwrite
    each other: News_Digest_DD_MM_YYYY_HHMM.pdf
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"News_Digest_{run_at.strftime('%d_%m_%Y_%H%M')}"
    candidate = stem
    counter = 2
    while (output_dir / f"{candidate}.pdf").exists() or (output_dir / f"{candidate}.json").exists():
        candidate = f"{stem}_{counter}"
        counter += 1
    return output_dir / f"{candidate}.pdf", output_dir / f"{candidate}.json"


def build_pdf(
    articles_by_source: dict[str, list[ArticleContent]],
    images: dict[str, bytes],
    output_path: Path,
    run_at: datetime,
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
        images: Prepared JPEG bytes keyed by article URL.
        output_path: Where the .pdf file will be saved.
        run_at: Time of this pipeline run; used on the cover page.

    Returns:
        Path to the saved .pdf file.

    Raises:
        DocumentBuildError: If the document cannot be saved.
    """
    styles = _get_styles()
    total = sum(len(v) for v in articles_by_source.values())
    story: list[Any] = []

    # --- Cover + TOC on first page ---
    story.append(Spacer(1, 1.2 * inch))
    story.append(Paragraph("DAILY TECH NEWS DIGEST", styles["cover_title"]))
    story.append(Paragraph(run_at.strftime("%A, %B %d, %Y"), styles["cover_sub"]))
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
    for index, (source_name, articles) in enumerate(articles_by_source.items()):
        key = f"src_{index}"

        heading_para = Paragraph(
            _clean(f"{source_name}  ({len(articles)} articles)"),
            styles["source_heading"],
        )
        heading_para._bookmark_key = key  # type: ignore[attr-defined]
        hr = HRFlowable(width="100%", thickness=1, color=colors.HexColor("#dddddd"), spaceAfter=4)

        for i, article in enumerate(articles):
            url = article.url
            title = _clean(article.title or "Untitled")
            title_html = (
                f'<link href="{_escape_attr(url)}" color="#0057b7"><b>{title}</b></link>'
                if url
                else f"<b>{title}</b>"
            )

            # For the first article, lead with the source heading + rule so the
            # heading is never left stranded at the bottom of a page without content.
            block: list[Any] = ([heading_para, hr] if i == 0 else [])
            block.append(Paragraph(title_html, styles["article_title"]))

            if article.published_date:
                block.append(Paragraph(_clean(article.published_date), styles["article_date"]))

            image_data = images.get(url)
            if image_data:
                img = _image_flowable(image_data)
                if img:
                    block.append(img)
                    block.append(Spacer(1, 4))

            if article.summary:
                for para in article.summary.split("\n"):
                    if para.strip():
                        block.append(Paragraph(_clean(para.strip()), styles["article_body"]))

            block.append(Spacer(1, 10))
            story.append(KeepTogether(block))

        story.append(Spacer(1, 6))

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
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
        output_path.unlink(missing_ok=True)
        raise DocumentBuildError(f"Failed to save document {output_path}: {exc}") from exc

    return output_path


def write_json(
    articles: list[ArticleContent], result: DigestRunResult, output_path: Path
) -> Path:
    """Write the run's articles and metadata as JSON (input for downstream agents).

    Raises:
        DocumentBuildError: If the file cannot be written.
    """
    payload = {
        "run": result.model_dump(mode="json", exclude={"site_reports"}),
        "articles": [a.model_dump(mode="json") for a in articles],
    }
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        raise DocumentBuildError(f"Failed to write {output_path}: {exc}") from exc
    logger.info("JSON export saved: %s", output_path)
    return output_path


def write_run_report(result: DigestRunResult, output_dir: Path) -> Path | None:
    """Write a plain-text report of every source and link that had a problem.

    Written on every run (even when there are no new articles), into
    <output_dir>/reports/Run_Report_DD_MM_YYYY_HHMM.txt. Returns None if it cannot be
    written (the problems are also in the log, so this never fails the run).
    """
    run_at = result.run_at or datetime.now()
    reports_dir = output_dir / "reports"
    path = reports_dir / f"Run_Report_{run_at.strftime('%d_%m_%Y_%H%M%S')}.txt"
    by_status: dict[str, list[Any]] = {}
    for report in result.site_reports:
        by_status.setdefault(report.status, []).append(report)

    lines = [
        f"News digest run report - {run_at.strftime('%A, %B %d, %Y %H:%M:%S')}",
        "=" * 72,
        f"Sources checked:          {len(result.site_reports)}",
        f"Sources OK:               {result.sources_processed}",
        f"Sources FAILED:           {len(by_status.get('failed', []))}",
        f"Sources skipped:          {len(by_status.get('skipped', []))}",
        f"Sources first scan:       {len(by_status.get('baselined', []))}",
        f"New articles in digest:   {result.total_articles}",
        f"Articles without content: {len(result.article_issues)}",
        f"Firecrawl credits used:   {result.firecrawl_credits_used}",
        f"OpenAI cost:              ${result.openai_cost_usd:.4f} ({result.openai_calls} calls, "
        f"{result.openai_input_tokens:,} input / {result.openai_output_tokens:,} output tokens)",
        f"Duration:                 {result.duration_seconds or 0:.0f}s",
        f"PDF:  {result.document_path or '-'}",
        f"JSON: {result.json_path or '-'}",
        "",
    ]

    def section(title: str, rows: list[str]) -> None:
        lines.append(f"{title} ({len(rows)})")
        lines.append("-" * 72)
        lines.extend(rows or ["(none)"])
        lines.append("")

    section(
        "FAILED SOURCES - no articles could be read; check the URL or add a feed",
        [f"* {r.source_name}\n    URL:    {r.url}\n    Reason: {r.detail}" for r in by_status.get("failed", [])],
    )
    section(
        "ARTICLES WITHOUT CONTENT - included in the digest with title + link only",
        [f"* [{i.source_name}] {i.title}\n    URL:    {i.url}\n    Reason: {i.problem}" for i in result.article_issues],
    )
    section(
        "SKIPPED SOURCES - not checked this run (will be retried automatically)",
        [f"* {r.source_name}: {r.detail}" for r in by_status.get("skipped", [])],
    )
    section(
        "ERRORS",
        list(result.errors),
    )
    section(
        "FIRST SCAN (BASELINE) - existing articles recorded; new ones appear from the next run",
        [f"* {r.source_name}: {r.articles_found} articles" for r in by_status.get("baselined", [])],
    )
    section(
        "ALL SOURCES",
        [
            f"{r.status:<10} {r.method or '-':<10} found={r.articles_found:<4} new={r.new_articles:<3} {r.source_name}"
            for r in result.site_reports
        ],
    )
    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        # Machine-readable copy of the run result (run history in the web UI).
        result.report_path = str(path)
        path.with_suffix(".json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    except OSError as exc:
        logger.error("Could not write run report %s: %s", path, exc)
        return None
    logger.info("Run report saved: %s", path)
    return path
