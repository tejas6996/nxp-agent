"""
Press-release (EE Herald article) endpoints used by the web UI.
"""

import html
import io
import re
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.dependency import SettingsDep
from app.models import PressReleaseDraft
from app.services.press_release import (
    PressReleaseError,
    display_date,
    find_draft_for_url,
    generate_press_release,
    image_dir,
    list_drafts,
    load_draft,
    refresh_images,
    save_draft,
    set_featured_image,
    to_markdown,
)

router = APIRouter(prefix="/press-releases", tags=["press-releases"])


class CreateRequest(BaseModel):
    url: str
    regenerate: bool = False
    source_title: str | None = None
    source_name: str | None = None


@router.post("", response_model=PressReleaseDraft)
async def create(body: CreateRequest, settings: SettingsDep) -> PressReleaseDraft:
    """Write an EE Herald-style article for a source URL (returns the saved draft if one
    already exists, unless regenerate=true)."""
    url = body.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Please enter a full http(s):// URL.")
    output_dir = Path(settings.output_dir)
    if not body.regenerate:
        existing = find_draft_for_url(url, output_dir)
        if existing is not None:
            return existing
    try:
        return await generate_press_release(url, settings, body.source_title, body.source_name)
    except PressReleaseError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc


@router.get("")
async def list_all(settings: SettingsDep) -> list[dict[str, Any]]:
    return [
        {
            "id": d.id,
            "created_at": d.created_at.isoformat(),
            "headline": d.headline,
            "section": d.section,
            "source_url": d.source_url,
            "source_name": d.source_name,
            "word_count": d.word_count,
            "warnings": len(d.warnings),
            "has_image": bool(d.featured_file),
            "openai_cost_usd": d.openai_cost_usd,
        }
        for d in list_drafts(Path(settings.output_dir))
    ]


@router.get("/{draft_id}", response_model=PressReleaseDraft)
async def get_one(draft_id: str, settings: SettingsDep) -> PressReleaseDraft:
    draft = load_draft(draft_id, Path(settings.output_dir))
    if draft is None:
        raise HTTPException(status_code=404, detail="Press release not found")
    return draft


def _require(draft_id: str, settings: Any) -> PressReleaseDraft:
    draft = load_draft(draft_id, Path(settings.output_dir))
    if draft is None:
        raise HTTPException(status_code=404, detail="Press release not found")
    return draft


class FeaturedRequest(BaseModel):
    index: int | None = None  # None = publish without an image


@router.post("/{draft_id}/featured", response_model=PressReleaseDraft)
async def choose_image(draft_id: str, body: FeaturedRequest, settings: SettingsDep) -> PressReleaseDraft:
    """Use another of the source's images (or none) for the article."""
    draft = _require(draft_id, settings)
    if body.index is not None and not (0 <= body.index < len(draft.images)):
        raise HTTPException(status_code=400, detail="No such image")
    output_dir = Path(settings.output_dir)
    set_featured_image(draft, body.index, output_dir)
    save_draft(draft, output_dir)
    return draft


@router.post("/{draft_id}/images/refresh", response_model=PressReleaseDraft)
async def find_images(draft_id: str, settings: SettingsDep) -> PressReleaseDraft:
    """Collect images from the source page again (article text is not changed)."""
    return await refresh_images(_require(draft_id, settings), settings)


@router.get("/{draft_id}/images/{file_name}")
async def get_image(draft_id: str, file_name: str, settings: SettingsDep, download: bool = False) -> FileResponse:
    if not re.fullmatch(r"[\w\-]+", draft_id) or not re.fullmatch(r"[\w\-]+\.(png|jpg)", file_name):
        raise HTTPException(status_code=404, detail="Image not found")
    path = image_dir(Path(settings.output_dir), draft_id) / file_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    media = "image/png" if path.suffix == ".png" else "image/jpeg"
    if download:
        return FileResponse(path, media_type=media, filename=f"{draft_id}-{file_name}")
    return FileResponse(path, media_type=media)


def _plain_text(draft: PressReleaseDraft) -> str:
    lines = [
        draft.headline,
        "",
        f"By {draft.byline} | {display_date(draft.created_at)} | {draft.read_minutes} min read",
        "",
    ]
    for number, paragraph in enumerate(draft.paragraphs, start=1):
        lines += [paragraph, ""]
        if number == draft.image_after_paragraph:
            if draft.featured_file:
                lines += [f"[Image: {draft.featured_file} - {draft.image_alt}]", ""]
            elif draft.image_note:
                lines += [f"[Image: {draft.image_note}]", ""]
    lines.append(f"Source: {draft.source_url}")
    return "\n".join(lines) + "\n"


def _html(draft: PressReleaseDraft, image_src: str | None = None) -> str:
    parts = []
    for number, paragraph in enumerate(draft.paragraphs, start=1):
        parts.append(f"<p>{html.escape(paragraph)}</p>")
        if number != draft.image_after_paragraph:
            continue
        if image_src:
            parts.append(
                f"<p><img src='{html.escape(image_src)}' alt='{html.escape(draft.image_alt)}' "
                "width='1200' height='800' style='max-width:100%;height:auto'></p>"
            )
        elif draft.image_note:
            parts.append(f"<p><em>[Image: {html.escape(draft.image_note)}]</em></p>")
    body = "\n".join(parts)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(draft.headline)}</title></head><body>"
        f"<h1>{html.escape(draft.headline)}</h1>"
        f"<p><em>By {html.escape(draft.byline)} | {display_date(draft.created_at)} | "
        f"{draft.read_minutes} min read</em></p>\n{body}\n"
        f"<p>Source: <a href='{html.escape(draft.source_url)}'>{html.escape(draft.source_url)}</a></p>"
        "</body></html>"
    )


def _source_image_url(draft: PressReleaseDraft) -> str | None:
    if draft.featured_index is None or draft.featured_index >= len(draft.images):
        return None
    return draft.images[draft.featured_index].source_url


def _zip(draft: PressReleaseDraft, settings: Any) -> bytes:
    """article.txt + article.html + article.md + featured image (1200x800) + original."""
    folder = image_dir(Path(settings.output_dir), draft.id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("article.txt", _plain_text(draft))
        zf.writestr(
            "article.html", _html(draft, draft.featured_file if draft.featured_file else None)
        )
        zf.writestr("article.md", to_markdown(draft).replace(f"]({draft.id}/", "]("))
        if draft.featured_file and (folder / draft.featured_file).is_file():
            zf.write(folder / draft.featured_file, draft.featured_file)
            original = draft.images[draft.featured_index or 0]
            if (folder / original.file).is_file():
                zf.write(folder / original.file, f"original-{original.file}")
    return buffer.getvalue()


@router.get("/{draft_id}/download")
async def download(draft_id: str, settings: SettingsDep, format: str = "txt") -> Response:
    draft = _require(draft_id, settings)
    if format == "zip":
        return Response(
            _zip(draft, settings),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{draft.id}.zip"'},
        )
    renderers = {
        "txt": (_plain_text, "text/plain; charset=utf-8"),
        "md": (to_markdown, "text/markdown; charset=utf-8"),
        "html": (lambda d: _html(d, _source_image_url(d)), "text/html; charset=utf-8"),
    }
    if format not in renderers:
        raise HTTPException(status_code=400, detail="format must be txt, md, html or zip")
    render, media = renderers[format]
    return Response(
        render(draft),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{draft.id}.{format}"'},
    )
