"""
"Add newsroom" from the EE Herald admin.

    POST /sources/check            {url, allow_firecrawl}  -> background check job
    GET  /sources/check/{job_id}                           -> status + result
    POST /sources/custom           {job_id, name, by}      -> add the checked newsroom
    POST /sources/custom/remove    {url, by}               -> remove an added newsroom

Only a newsroom that passed a check can be added (the add call refers to the check
job; the browser never supplies the URLs that get stored). Every fetch goes through
the URL guard, Firecrawl is only used when the editor asked for it, and at most two
checks run at the same time.
"""

import asyncio
import logging
import time
import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.dependency import SettingsDep
from app.infra.http import HttpFetcher
from app.infra.openai import OpenAIClient
from app.infra.url_guard import UnsafeURLError, normalise_user_url, resolve_public
from app.services.discovery import ADDABLE, check_newsroom, mostly_non_english
from app.services.parsing import parse_date
from app.services.pipeline import check_sites
from app.services.site_settings import add_custom_site, clean_name, find_tracked, remove_custom_site

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sources", tags=["sources"])

_JOBS: dict[str, dict[str, Any]] = {}
_JOB_TTL_SECONDS = 2 * 3600
_MAX_JOBS = 200
_CHECK_SLOTS = asyncio.Semaphore(2)
_CHECK_TIMEOUT_SECONDS = 240


class CheckRequest(BaseModel):
    url: str = Field(max_length=2048)
    allow_firecrawl: bool = False


class AddRequest(BaseModel):
    job_id: str = Field(max_length=64)
    name: str = Field(max_length=200)
    by: str = Field(default="", max_length=60)


class RemoveRequest(BaseModel):
    url: str = Field(max_length=2048)
    by: str = Field(default="", max_length=60)


def _prune() -> None:
    now = time.time()
    for job_id in [k for k, j in _JOBS.items() if j["status"] != "running" and now - j["updated"] > _JOB_TTL_SECONDS]:
        _JOBS.pop(job_id, None)
    while len(_JOBS) > _MAX_JOBS:
        _JOBS.pop(min(_JOBS, key=lambda k: _JOBS[k]["updated"]), None)


def _public_view(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": job_id, "status": job["status"], "url": job["url"], "result": job.get("result"), "error": job.get("error")}


async def _run_check(job_id: str, url: str, allow_firecrawl: bool, settings: Any) -> None:
    job = _JOBS[job_id]
    try:
        async with _CHECK_SLOTS:
            llm = OpenAIClient(settings)
            async with HttpFetcher(settings) as http:
                found = await asyncio.wait_for(
                    check_newsroom(http, llm, asyncio.Semaphore(2), url, date.today()),
                    timeout=_CHECK_TIMEOUT_SECONDS,
                )
            firecrawl_credits = 0
            listing = found.get("listing") or found.get("final_url") or url
            if found.get("verdict") == "needs_firecrawl" and allow_firecrawl:
                scans, firecrawl_credits, _ = await check_sites(
                    settings, [{"name": found.get("name_guess") or "New source", "url": listing,
                                "firecrawl_cooldown_hours": 24}], allow_firecrawl=True,
                )
                scan = scans[0]
                if scan.status == "ok" and scan.articles:
                    dates = [d for d in (parse_date(a.date) for a in scan.articles) if d]
                    # The retry reads the free way first; only call it Firecrawl when it was.
                    used = scan.method if scan.method in ("feed", "direct", "firecrawl") else "firecrawl"
                    found.update(
                        verdict=used, method=used, listing=listing, items=len(scan.articles),
                        newest=max(dates).isoformat() if dates else None,
                        titles=[a.title for a in scan.articles[:3]],
                        samples=[{"title": a.title, "url": a.url, "date": a.date} for a in scan.articles[:3]],
                    )
                else:
                    found.update(verdict="unreadable", why=scan.detail or "Firecrawl could not read it either")
        result = {
            "verdict": found.get("verdict"),
            "why": found.get("why"),
            "notes": found.get("notes") or [],
            "name_guess": (found.get("name_guess") or "")[:80],
            "listing": found.get("listing") or found.get("final_url"),
            "feed": found.get("feed") if found.get("method") == "feed" else None,
            "method": found.get("method"),
            "items": found.get("items"),
            "newest": found.get("newest"),
            "samples": found.get("samples") or [],
            "non_english": mostly_non_english(found.get("titles") or []),
            "firecrawl_credits_used": firecrawl_credits,
        }
        # A home page or redirect can lead to a newsroom that is already tracked.
        dup = find_tracked([result["listing"] or "", result["feed"] or ""], settings)
        if dup:
            result["verdict"] = "duplicate"
            result["duplicate_of"] = dup["name"]
        addable = result["verdict"] in ADDABLE or result["verdict"] == "firecrawl"
        result["addable"] = addable
        if addable:
            job["candidate"] = {
                "url": result["listing"],
                "feed": result["feed"],
                "firecrawl": result["verdict"] == "firecrawl",
            }
        job.update(status="done", result=result)
    except UnsafeURLError as exc:
        job.update(status="error", error=str(exc))
    except asyncio.TimeoutError:
        job.update(status="error", error="The website took too long to answer. Try again later or use its RSS feed link.")
    except Exception as exc:  # noqa: BLE001 - the editor gets a message, details stay in the log
        logger.exception("Source check failed for %s", url)
        job.update(status="error", error=f"The check failed ({type(exc).__name__}). Please try again.")
    finally:
        job["updated"] = time.time()


@router.post("/check", status_code=202)
async def start_check(body: CheckRequest, settings: SettingsDep) -> dict[str, Any]:
    try:
        url = normalise_user_url(body.url)
        await resolve_public(url)  # immediate, clear answer for private / unknown hosts
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    dup = find_tracked([url], settings)
    _prune()
    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {"status": "running", "url": url, "updated": time.time()}
    if dup:
        _JOBS[job_id].update(status="done", result={
            "verdict": "duplicate", "duplicate_of": dup["name"], "addable": False, "notes": [], "samples": [],
            "listing": dup["url"], "feed": dup.get("feed"),
        })
        return _public_view(job_id, _JOBS[job_id])
    if sum(1 for j in _JOBS.values() if j["status"] == "running") > 6:
        _JOBS.pop(job_id, None)
        raise HTTPException(status_code=429, detail="Several checks are already running. Try again in a minute.")
    asyncio.create_task(_run_check(job_id, url, body.allow_firecrawl, settings))
    return _public_view(job_id, _JOBS[job_id])


@router.get("/check/{job_id}")
async def check_status(job_id: str) -> dict[str, Any]:
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="This check has expired. Please check the link again.")
    return _public_view(job_id, job)


@router.post("/custom", status_code=201)
async def add_source(body: AddRequest, settings: SettingsDep) -> dict[str, Any]:
    job = _JOBS.get(body.job_id)
    if job is None or job["status"] != "done" or not job.get("candidate"):
        raise HTTPException(status_code=400, detail="Check the link first; only a newsroom that passed the check can be added.")
    try:
        name = clean_name(body.name)
        entry = add_custom_site(settings, {**job["candidate"], "name": name}, by=body.by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job["candidate"] = None  # one add per check
    logger.info("Source %s (%s) added by %s.", entry["name"], entry["url"], body.by or "?")
    return entry


@router.post("/custom/remove")
async def remove_source(body: RemoveRequest, settings: SettingsDep) -> dict[str, Any]:
    if not remove_custom_site(settings, body.url):
        raise HTTPException(status_code=404, detail="Only newsrooms added from the admin can be removed.")
    logger.info("Source %s removed by %s.", body.url, body.by or "?")
    return {"removed": True, "url": body.url}
