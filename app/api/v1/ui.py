"""
Endpoints used by the web UI (served at /).

All file access is restricted to OUTPUT_DIR.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.dependency import SettingsDep
from app.infra.firecrawl import FirecrawlClient
from app.services.parsing import url_key
from app.services.pipeline import check_sites
from app.services.run_manager import RunManager, next_scheduled_run, parse_run_times
from app.services.state import load_state
from app.sites import SITES, select_sites

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"])

_credit_cache: dict[str, Any] = {"at": 0.0, "value": None}
_CREDIT_CACHE_SECONDS = 120


def _manager(request: Request) -> RunManager:
    return request.app.state.run_manager


def _output_dir(settings: Any) -> Path:
    return Path(settings.output_dir).resolve()


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _rel(path_str: str | None, base: Path) -> str | None:
    """Path relative to OUTPUT_DIR (for /files links), or None if outside / missing."""
    if not path_str:
        return None
    try:
        path = Path(path_str)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve().relative_to(base).as_posix()
    except (ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.get("/status")
async def status(request: Request, settings: SettingsDep, since: int = 0) -> dict[str, Any]:
    """Live status of the current/last UI run, with log lines newer than `since`."""
    snapshot = _manager(request).snapshot(since)
    times = parse_run_times(settings.auto_run_times)
    nxt = next_scheduled_run(times)
    snapshot["schedule"] = {"times": times, "next": nxt.isoformat() if nxt else None}
    lock = Path(settings.state_file).with_name(Path(settings.state_file).name + ".lock")
    snapshot["locked_by_other_run"] = lock.exists() and not snapshot["running"]
    return snapshot


@router.post("/runs", status_code=202)
async def start_run(request: Request, settings: SettingsDep) -> dict[str, Any]:
    """Start a pipeline run in the background."""
    if not _manager(request).start(settings, trigger="ui"):
        raise HTTPException(status_code=409, detail="A run is already in progress.")
    return {"started": True}


@router.get("/runs")
async def run_history(settings: SettingsDep, limit: int = 50) -> list[dict[str, Any]]:
    """Past runs (newest first), read from output_dir/reports/Run_Report_*.json."""
    base = _output_dir(settings)
    reports = sorted((base / "reports").glob("Run_Report_*.json"), reverse=True)[:limit]
    runs = []
    for path in reports:
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        data.pop("site_reports", None)
        data["report_file"] = _rel(str(path.with_suffix(".txt")), base)
        data["pdf_file"] = _rel(data.get("document_path"), base)
        data["json_file"] = _rel(data.get("json_path"), base)
        runs.append(data)
    return runs


@router.get("/runs/latest/sources")
async def latest_site_reports(settings: SettingsDep) -> list[dict[str, Any]]:
    base = _output_dir(settings)
    reports = sorted((base / "reports").glob("Run_Report_*.json"), reverse=True)
    for path in reports:
        data = _read_json(path)
        if isinstance(data, dict) and data.get("site_reports"):
            return data["site_reports"]
    return []


# ---------------------------------------------------------------------------
# Digests and articles
# ---------------------------------------------------------------------------


@router.get("/digests")
async def digests(settings: SettingsDep) -> list[dict[str, Any]]:
    """All digests in OUTPUT_DIR (newest first) with their PDF / JSON files."""
    base = _output_dir(settings)
    stems: dict[str, dict[str, Any]] = {}
    for path in list(base.glob("News_Digest_*.pdf")) + list(base.glob("News_Digest_*.json")):
        entry = stems.setdefault(path.stem, {"name": path.stem, "pdf": None, "json": None})
        entry["pdf" if path.suffix == ".pdf" else "json"] = path.name
        entry["modified"] = max(entry.get("modified", 0), path.stat().st_mtime)
    items = sorted(stems.values(), key=lambda e: e["modified"], reverse=True)
    for item in items:
        item["modified"] = datetime.fromtimestamp(item["modified"]).isoformat(timespec="seconds")
        if item["json"]:
            data = _read_json(base / item["json"])
            item["articles"] = len(data.get("articles", [])) if isinstance(data, dict) else None
    return items


@router.get("/articles")
async def articles(settings: SettingsDep, days: int = 7) -> list[dict[str, Any]]:
    """Articles from the JSON exports of the last `days` days (newest digest first, deduplicated)."""
    base = _output_dir(settings)
    cutoff = time.time() - days * 86400
    exports = sorted(
        (p for p in base.glob("News_Digest_*.json") if p.stat().st_mtime >= cutoff),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for path in exports:
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        for article in data.get("articles", []):
            key = url_key(article.get("url", ""))
            if key in seen:
                continue
            seen.add(key)
            article["digest"] = path.stem
            out.append(article)
    return out


@router.get("/files/{file_path:path}")
async def get_file(file_path: str, settings: SettingsDep, download: bool = False) -> FileResponse:
    """Serve a file from OUTPUT_DIR (PDF opens in the browser unless download=true)."""
    base = _output_dir(settings)
    target = (base / file_path).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if target.suffix.lower() not in (".pdf", ".json", ".txt"):
        raise HTTPException(status_code=404, detail="File not found")
    media = {".pdf": "application/pdf", ".json": "application/json", ".txt": "text/plain; charset=utf-8"}
    if download:
        return FileResponse(target, media_type=media[target.suffix.lower()], filename=target.name)
    return FileResponse(target, media_type=media[target.suffix.lower()])


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@router.get("/sources")
async def sources(settings: SettingsDep) -> list[dict[str, Any]]:
    """Configured sites with what the state file knows about them."""
    try:
        state = load_state(Path(settings.state_file), SITES)
    except Exception:  # noqa: BLE001
        state = {"sites": {}}
    latest = {r["url"]: r for r in await latest_site_reports(settings)}
    rows = []
    for site in SITES:
        site_state = state.get("sites", {}).get(site["url"], {})
        report = latest.get(site["url"], {})
        rows.append(
            {
                "name": site["name"],
                "url": site["url"],
                "feed": site.get("feed"),
                "method": site_state.get("method") or report.get("method"),
                "baselined": bool(site_state.get("baselined")),
                "last_success_at": site_state.get("last_success_at"),
                "last_firecrawl_at": site_state.get("last_firecrawl_at"),
                "last_status": report.get("status"),
                "last_detail": report.get("detail"),
                "last_new_articles": report.get("new_articles"),
            }
        )
    return rows


class CheckRequest(BaseModel):
    sites: str
    allow_firecrawl: bool = False


@router.post("/check")
async def check(body: CheckRequest, settings: SettingsDep) -> dict[str, Any]:
    """Preview what would be extracted from matching sites (no state change, no PDF)."""
    selected = select_sites(body.sites)
    if not selected:
        raise HTTPException(status_code=404, detail=f"No site matches {body.sites!r}")
    if len(selected) > 25 and body.allow_firecrawl:
        raise HTTPException(status_code=400, detail="Firecrawl previews are limited to 25 sites.")
    scans, credits, usage = await check_sites(settings, selected, body.allow_firecrawl)
    return {
        "firecrawl_credits_used": credits,
        "openai_cost_usd": round(usage.cost_usd, 6),
        "results": [
            {
                "name": s.site["name"],
                "url": s.site["url"],
                "status": s.status,
                "method": s.method,
                "detail": s.detail,
                "articles": [a.model_dump() for a in s.articles],
            }
            for s in scans
        ],
    }


# ---------------------------------------------------------------------------
# Credits and configuration
# ---------------------------------------------------------------------------


@router.get("/credits")
async def credits(settings: SettingsDep, refresh: bool = False) -> dict[str, Any]:
    """Firecrawl balance (free API call, cached for two minutes)."""
    if not settings.firecrawl_api_key or not settings.firecrawl_enabled:
        return {"available": False}
    if refresh or time.time() - _credit_cache["at"] > _CREDIT_CACHE_SECONDS:
        balance = await FirecrawlClient(settings).remaining_credits()
        _credit_cache.update(at=time.time(), value=balance)
    balance = _credit_cache["value"]
    if not balance:
        return {"available": False}
    remaining, period_end = balance
    days_left = None
    if period_end is not None:
        days_left = max(0.0, (period_end - datetime.now(period_end.tzinfo)) / timedelta(days=1))
    return {
        "available": True,
        "remaining": remaining,
        "period_end": period_end.isoformat() if period_end else None,
        "days_left": round(days_left, 1) if days_left is not None else None,
    }


@router.get("/config")
async def config(settings: SettingsDep) -> dict[str, Any]:
    return {
        "model": settings.openai_model,
        "reasoning_effort": settings.openai_reasoning_effort,
        "price_input_per_m": settings.openai_price_input_per_m,
        "price_output_per_m": settings.openai_price_output_per_m,
        "sites": len(SITES),
        "feeds": sum(1 for s in SITES if s.get("feed")),
        "lookback_days": settings.lookback_days,
        "firecrawl_enabled": settings.firecrawl_enabled and bool(settings.firecrawl_api_key),
        "firecrawl_max_credits_per_run": settings.firecrawl_max_credits_per_run,
        "firecrawl_runs_per_day": settings.firecrawl_runs_per_day,
        "firecrawl_cooldown_hours": settings.firecrawl_listing_cooldown_hours,
        "auto_run_times": parse_run_times(settings.auto_run_times),
        "output_dir": str(_output_dir(settings)),
    }
