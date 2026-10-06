"""
Press-release (EE Herald article) endpoints used by the web UI.
"""

import html
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.dependency import SettingsDep
from app.models import PressReleaseDraft
from app.services.press_release import (
    PressReleaseError,
    display_date,
    find_draft_for_url,
    generate_press_release,
    list_drafts,
    load_draft,
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


def _plain_text(draft: PressReleaseDraft) -> str:
    lines = [
        draft.headline,
        "",
        f"By {draft.byline} | {display_date(draft.created_at)} | {draft.read_minutes} min read",
        "",
    ]
    for paragraph in draft.paragraphs:
        lines += [paragraph, ""]
    lines.append(f"Source: {draft.source_url}")
    return "\n".join(lines) + "\n"


def _html(draft: PressReleaseDraft) -> str:
    body = "\n".join(f"<p>{html.escape(p)}</p>" for p in draft.paragraphs)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(draft.headline)}</title></head><body>"
        f"<h1>{html.escape(draft.headline)}</h1>"
        f"<p><em>By {html.escape(draft.byline)} | {display_date(draft.created_at)} | "
        f"{draft.read_minutes} min read</em></p>\n{body}\n"
        f"<p>Source: <a href='{html.escape(draft.source_url)}'>{html.escape(draft.source_url)}</a></p>"
        "</body></html>"
    )


@router.get("/{draft_id}/download")
async def download(draft_id: str, settings: SettingsDep, format: str = "txt") -> Response:
    draft = load_draft(draft_id, Path(settings.output_dir))
    if draft is None:
        raise HTTPException(status_code=404, detail="Press release not found")
    renderers = {
        "txt": (_plain_text, "text/plain; charset=utf-8"),
        "md": (to_markdown, "text/markdown; charset=utf-8"),
        "html": (_html, "text/html; charset=utf-8"),
    }
    if format not in renderers:
        raise HTTPException(status_code=400, detail="format must be txt, md or html")
    render, media = renderers[format]
    return Response(
        render(draft),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{draft.id}.{format}"'},
    )
