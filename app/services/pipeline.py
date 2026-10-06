"""
Main pipeline orchestrator.

This module is the primary entrypoint called by both the FastAPI /run endpoint and the
standalone app/run.py script.

Stages:
    1. Scan every newsroom (feed -> direct fetch -> Firecrawl fallback).
    2. Keep only articles not seen before. A site scanned for the first time is
       "baselined": its current articles are recorded silently so the next digest only
       contains genuinely new items. Articles older than LOOKBACK_DAYS are dropped.
    3. Fetch each new article page and extract its opening text.
    4. Build the PDF digest and its JSON export.
    5. Save state. Articles are only marked as seen once the digest has been written, so
       nothing is lost if a run fails part-way.
"""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.exceptions import DocumentBuildError
from app.infra.firecrawl import FirecrawlBudget, FirecrawlClient
from app.infra.http import HttpFetcher
from app.infra.openai import OpenAIClient, TokenUsage
from app.models import (
    ArticleContent,
    ArticleIssue,
    ArticleListing,
    DigestRunResult,
    SiteReport,
)
from app.services.agent import extract_article
from app.services.crawling import Crawler, SiteScan
from app.services.create_doc import (
    build_pdf,
    output_paths,
    prepare_image,
    write_json,
    write_run_report,
)
from app.services.parsing import parse_date, url_key
from app.services.state import load_state, run_lock, save_state
from app.settings import Settings
from app.sites import SITES

logger = logging.getLogger(__name__)


async def run_pipeline(
    settings: Settings, sites: list[dict[str, str]] | None = None, trigger: str = "api"
) -> DigestRunResult:
    """Execute the full news scraping and digest generation pipeline.

    Raises:
        PipelineBusyError: If another run is using the same state file.
        StatePersistenceError: If the state file cannot be read or written.
    """
    sites = dedupe_sites(sites if sites is not None else SITES)
    state_path = Path(settings.state_file)
    with run_lock(state_path):
        return await _run(settings, sites, state_path, trigger)


async def check_sites(
    settings: Settings, sites: list[dict[str, str]], allow_firecrawl: bool = False
) -> tuple[list[SiteScan], int, TokenUsage]:
    """Scan sites and return what would be extracted, without touching state or output.

    Used to verify a newly added or changed newsroom. Firecrawl is only used when
    explicitly allowed.

    Returns:
        (scans, firecrawl_credits_used, openai_usage)
    """
    run_at = datetime.now().replace(microsecond=0)
    llm = OpenAIClient(settings)
    firecrawl: FirecrawlClient | None = None
    if allow_firecrawl and settings.firecrawl_enabled and settings.firecrawl_api_key:
        firecrawl = FirecrawlClient(settings)
    budget = await FirecrawlBudget.create(firecrawl, settings)
    async with HttpFetcher(settings) as http:
        crawler = Crawler(
            settings,
            http,
            firecrawl,
            budget,
            llm,
            asyncio.Semaphore(max(1, settings.max_concurrent_openai)),
            {},  # fresh state: always run full extraction
            run_at.date(),
            run_at,
        )
        scans = await crawler.scan_sites(dedupe_sites(sites))
    return scans, budget.used, llm.usage


async def _run(
    settings: Settings, sites: list[dict[str, str]], state_path: Path, trigger: str
) -> DigestRunResult:
    started = time.monotonic()
    run_at = datetime.now().replace(microsecond=0)
    today = run_at.date()
    errors: list[str] = []

    state = load_state(state_path, sites)
    seen: dict[str, str] = state["seen_urls"]
    site_states: dict[str, dict[str, Any]] = state["sites"]

    llm = OpenAIClient(settings)
    firecrawl: FirecrawlClient | None = None
    if settings.firecrawl_enabled and settings.firecrawl_api_key:
        firecrawl = FirecrawlClient(settings)
    budget = await FirecrawlBudget.create(firecrawl, settings)
    llm_semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_openai))

    async with HttpFetcher(settings) as http:
        crawler = Crawler(
            settings, http, firecrawl, budget, llm, llm_semaphore, site_states, today, run_at
        )

        # Stage 1: scan listings
        logger.info("Stage 1: Scanning %d newsrooms.", len(sites))
        scans = await crawler.scan_sites(sites)

        # Stage 2: select new articles
        to_process, reports = _select_new_articles(scans, seen, site_states, settings, today, run_at)
        logger.info(
            "Stage 2 complete. %d new articles to process (%d URLs already tracked).",
            len(to_process),
            len(seen),
        )

        articles: list[ArticleContent] = []
        article_issues: list[ArticleIssue] = []
        images: dict[str, bytes] = {}
        if to_process:
            # Stage 3: fetch and extract article pages
            logger.info("Stage 3: Fetching %d article pages.", len(to_process))
            processed = await asyncio.gather(
                *(_process_article(crawler, llm, llm_semaphore, a, today) for a in to_process)
            )
            cutoff = today - timedelta(days=settings.lookback_days)
            for listing, (article, problem) in zip(to_process, processed):
                published = parse_date(article.published_date)
                if published and published < cutoff:
                    logger.info("Skipping old article (%s): %s", published, article.url)
                    seen[url_key(listing.url)] = today.isoformat()
                    continue
                articles.append(article)
                if problem:
                    article_issues.append(
                        ArticleIssue(
                            source_name=article.source_name,
                            title=article.title,
                            url=article.url,
                            problem=problem,
                        )
                    )
            logger.info("Stage 3 complete. %d articles ready.", len(articles))

            # Download images while the HTTP session is open.
            images = await _download_images(http, articles)

    result = DigestRunResult(
        run_date=today,
        run_at=run_at,
        total_articles=len(articles),
        sources_processed=sum(1 for s in scans if s.status in ("ok", "unchanged")),
        firecrawl_credits_used=budget.used,
        trigger=trigger,
        failed_sources=[f"{s.site['name']}: {s.detail}" for s in scans if s.status == "failed"],
        site_reports=reports,
        article_issues=article_issues,
        errors=errors,
    )

    # Stage 4: build documents
    if articles:
        logger.info("Stage 4: Building PDF digest with %d articles.", len(articles))
        by_source: dict[str, list[ArticleContent]] = defaultdict(list)
        for article in articles:
            by_source[article.source_name].append(article)
        pdf_path, json_path = output_paths(Path(settings.output_dir), run_at)
        try:
            await asyncio.to_thread(build_pdf, dict(by_source), images, pdf_path, run_at)
            result.document_path = str(pdf_path)
        except DocumentBuildError as exc:
            logger.error("Document build failed: %s", exc)
            errors.append(str(exc))
        try:
            write_json(articles, result, json_path)
            result.json_path = str(json_path)
        except DocumentBuildError as exc:
            logger.error("JSON export failed: %s", exc)
            errors.append(str(exc))

        # Only articles that reached the digest are marked as seen; if the PDF failed
        # they will be retried on the next run.
        if result.document_path:
            for article in articles:
                seen[url_key(article.url)] = today.isoformat()

    # Stage 5: save state
    state["last_run_date"] = today.isoformat()
    state["last_run_at"] = run_at.isoformat()
    save_state(state_path, state)
    result.errors = errors
    result.openai_calls = llm.usage.calls
    result.openai_input_tokens = llm.usage.input_tokens
    result.openai_cached_input_tokens = llm.usage.cached_input_tokens
    result.openai_output_tokens = llm.usage.output_tokens
    result.openai_cost_usd = round(llm.usage.cost_usd, 6)
    result.duration_seconds = round(time.monotonic() - started, 1)
    logger.info(
        "OpenAI usage: %d calls, %d input tokens (%d cached), %d output tokens = $%.4f",
        llm.usage.calls,
        llm.usage.input_tokens,
        llm.usage.cached_input_tokens,
        llm.usage.output_tokens,
        llm.usage.cost_usd,
    )

    # Stage 6: report every failed source / link (written on every run).
    report_path = write_run_report(result, Path(settings.output_dir))
    result.report_path = str(report_path) if report_path else None
    return result


def dedupe_sites(sites: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop duplicate entries (same listing URL or same name) with a warning."""
    unique: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    seen_names: set[str] = set()
    for site in sites:
        key = url_key(site["url"])
        name = site["name"].strip().casefold()
        if key in seen_urls or name in seen_names:
            logger.warning("Duplicate site entry ignored: %s (%s)", site["name"], site["url"])
            continue
        seen_urls.add(key)
        seen_names.add(name)
        unique.append(site)
    return unique


def _select_new_articles(
    scans: list[SiteScan],
    seen: dict[str, str],
    site_states: dict[str, dict[str, Any]],
    settings: Settings,
    today: date,
    run_at: datetime,
) -> tuple[list[ArticleListing], list[SiteReport]]:
    """Update per-site state from the scans and return the articles to process."""
    cutoff = today - timedelta(days=settings.lookback_days)
    today_str = today.isoformat()
    selected: list[ArticleListing] = []
    selected_keys: set[str] = set()
    reports: list[SiteReport] = []

    for scan in scans:
        site = scan.site
        site_state = site_states.setdefault(site["url"], {})
        report = SiteReport(
            source_name=site["name"],
            url=site["url"],
            method=scan.method,
            articles_found=len(scan.articles),
            status=scan.status,  # type: ignore[arg-type]
            detail=scan.detail,
        )
        reports.append(report)

        if scan.firecrawl_used:
            site_state["last_firecrawl_at"] = run_at.isoformat()
        if scan.status not in ("ok", "unchanged"):
            if scan.status == "failed":
                logger.warning("%s: FAILED - %s", site["name"], scan.detail)
            else:
                logger.info("%s: skipped - %s", site["name"], scan.detail)
            continue

        site_state["method"] = scan.method
        site_state["method_checked"] = today_str
        site_state["last_success_at"] = run_at.isoformat()
        if scan.link_fingerprint is not None:
            site_state["last_links"] = scan.link_fingerprint

        if scan.status == "unchanged":
            continue

        if not site_state.get("baselined"):
            for article in scan.articles:
                seen.setdefault(url_key(article.url), today_str)
            site_state["baselined"] = True
            report.status = "baselined"
            report.detail = (
                f"First scan: recorded {len(scan.articles)} existing articles; "
                "new ones will appear in the next digests."
            )
            logger.info("%s: baselined with %d existing articles.", site["name"], len(scan.articles))
            continue

        fresh: list[ArticleListing] = []
        for article in scan.articles:
            key = url_key(article.url)
            if key in seen or key in selected_keys:
                continue
            published = parse_date(article.date)
            if published and published < cutoff:
                # An old item that only now became visible (e.g. a layout change): record it
                # but don't put it in the digest.
                seen[key] = today_str
                continue
            fresh.append(article)

        if len(fresh) > settings.max_articles_per_site:
            logger.warning(
                "%s: %d new articles exceeds MAX_ARTICLES_PER_SITE=%d; keeping the first %d.",
                site["name"],
                len(fresh),
                settings.max_articles_per_site,
                settings.max_articles_per_site,
            )
            for article in fresh[settings.max_articles_per_site:]:
                seen[url_key(article.url)] = today_str
            fresh = fresh[: settings.max_articles_per_site]

        for article in fresh:
            selected_keys.add(url_key(article.url))
        selected.extend(fresh)
        report.new_articles = len(fresh)

    return selected, reports


async def _process_article(
    crawler: Crawler,
    llm: OpenAIClient,
    llm_semaphore: asyncio.Semaphore,
    listing: ArticleListing,
    today: date,
) -> tuple[ArticleContent, str | None]:
    """Fetch and extract one article.

    Never raises: on any failure the article is kept with its title, link and date so it
    still appears in the digest, and the problem is returned for the run report.
    """
    article = ArticleContent(
        title=listing.title,
        url=listing.url,
        source_name=listing.source_name,
        source_url=listing.source_url,
        published_date=listing.date,
    )
    try:
        page, via, problem = await crawler.fetch_article(listing)
        if page is None:
            logger.warning("No content for %s (%s) - title + link only.", listing.url, problem)
            return article, problem or "no content"
        async with llm_semaphore:
            data = await extract_article(llm, listing, page, today)

        if not data["summary"] and via == "direct":
            # The fetched HTML had no article body (it is loaded by JavaScript): try
            # rendering the page with Firecrawl if this run's credit allowance permits.
            rendered, fc_problem = await crawler.fetch_article_firecrawl(listing)
            if rendered is not None and rendered.text:
                async with llm_semaphore:
                    rendered_data = await extract_article(llm, listing, rendered, today)
                if rendered_data["summary"]:
                    page, via, data = rendered, "firecrawl", rendered_data
            if not data["summary"]:
                problem = "article body not found in page " + (
                    f"({fc_problem})" if fc_problem else "(also after Firecrawl rendering)"
                )

        article.title = data["title"] or listing.title
        article.published_date = data["date"]
        article.summary = data["summary"] or None
        article.image_url = page.image_url
        article.body_text = page.text or None
        article.fetched_via = via
        return article, problem if not article.summary else None
    except Exception as exc:  # noqa: BLE001 - keep the article whatever happens
        logger.warning("Article processing failed for %s: %s", listing.url, exc)
        return article, f"processing error: {exc}"


async def _download_images(http: HttpFetcher, articles: list[ArticleContent]) -> dict[str, bytes]:
    """Download and prepare lead images concurrently. Failures just mean no image."""

    async def one(article: ArticleContent) -> tuple[str, bytes | None]:
        if not article.image_url:
            return article.url, None
        try:
            result = await http.fetch(article.image_url, referer=article.url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Image download failed for %s: %s", article.image_url, exc)
            return article.url, None
        if result is None or not result.ok:
            logger.debug("Image download failed for %s", article.image_url)
            return article.url, None
        return article.url, await asyncio.to_thread(prepare_image, result.content)

    pairs = await asyncio.gather(*(one(a) for a in articles))
    images = {url: data for url, data in pairs if data}
    logger.info("Prepared %d/%d article images.", len(images), len(articles))
    return images
