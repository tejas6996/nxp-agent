"""
Site scanning and article fetching.

Each newsroom is read with the cheapest method that works, in this order:

1. RSS/Atom feed (if configured in sites.py)  - free, exact titles/links/dates, no LLM.
2. Direct fetch with a real-browser fingerprint - free.
3. Firecrawl                                     - costs credits, only for pages that are
                                                   JavaScript-only or blocked, and only
                                                   within the run's credit allowance.

The method that worked is remembered per site in the state file, so sites that need
Firecrawl don't waste a direct attempt every run (they are re-checked periodically).
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.exceptions import FirecrawlClientError, OpenAIClientError
from app.infra.firecrawl import FirecrawlBudget, FirecrawlClient
from app.infra.http import FetchResult, HttpFetcher
from app.infra.openai import OpenAIClient
from app.models import ArticleListing, FetchMethod
from app.services.agent import extract_listing
from app.services.parsing import (
    ArticlePage,
    LinkDigest,
    build_link_digest,
    extract_article_page,
    parse_feed,
)
from app.settings import Settings

logger = logging.getLogger(__name__)

# A listing page with fewer unique links than this is almost certainly a JavaScript shell.
_MIN_LISTING_LINKS = 5
# Article text shorter than this means the body is rendered client-side.
_MIN_ARTICLE_CHARS = 300


def link_fingerprint(keys: set[str]) -> list[str]:
    """Compact, stable representation of a page's link set for the state file."""
    return sorted(hashlib.blake2b(k.encode("utf-8"), digest_size=8).hexdigest() for k in keys)


@dataclass
class SiteScan:
    """Outcome of scanning one newsroom listing."""

    site: dict[str, str]
    status: str  # ok | unchanged | skipped | failed | needs_firecrawl (internal)
    method: FetchMethod | None = None
    articles: list[ArticleListing] = field(default_factory=list)
    detail: str | None = None
    link_fingerprint: list[str] | None = None
    firecrawl_used: bool = False


class Crawler:
    """Scans newsroom listings and fetches article pages using the cheapest working method."""

    def __init__(
        self,
        settings: Settings,
        http: HttpFetcher,
        firecrawl: FirecrawlClient | None,
        budget: FirecrawlBudget,
        llm: OpenAIClient,
        llm_semaphore: asyncio.Semaphore,
        site_states: dict[str, dict[str, Any]],
        today: date,
        now: datetime,
    ) -> None:
        self._settings = settings
        self._http = http
        self._firecrawl = firecrawl
        self._budget = budget
        self._llm = llm
        self._llm_semaphore = llm_semaphore
        self._site_states = site_states
        self._today = today
        self._now = now

    # ------------------------------------------------------------------
    # Listing pages
    # ------------------------------------------------------------------

    async def scan_sites(self, sites: list[dict[str, str]]) -> list[SiteScan]:
        """Scan all sites: free methods concurrently, then Firecrawl for the rest.

        Firecrawl candidates are served in order of how long ago they were last scraped
        with Firecrawl (never-scraped first), so when the credit allowance cannot cover
        every JavaScript-only site in one run, the remaining ones go first next run and
        no site is starved. Returns scans in the same order as `sites`.
        """
        scans = list(await asyncio.gather(*(self.scan_site_free(s) for s in sites)))
        pending = [i for i, scan in enumerate(scans) if scan.status == "needs_firecrawl"]
        pending.sort(
            key=lambda i: self._site_states.get(sites[i]["url"], {}).get("last_firecrawl_at") or ""
        )
        # Tasks are created in priority order and claim credits before their first await,
        # so the budget is handed out in exactly this order.
        results = await asyncio.gather(*(self.scan_site_firecrawl(scans[i]) for i in pending))
        for i, result in zip(pending, results):
            scans[i] = result
        return scans

    async def scan_site_free(self, site: dict[str, str]) -> SiteScan:
        """Scan one site with feed/direct fetching only. Never raises.

        Returns a SiteScan with status "needs_firecrawl" (and the reasons in `detail`)
        when the free methods did not work.
        """
        try:
            return await self._scan_site_free(site)
        except Exception as exc:  # noqa: BLE001 - one bad site must not stop the run
            logger.exception("Unexpected error scanning %s", site["name"])
            return SiteScan(site=site, status="failed", detail=f"Unexpected error: {exc}")

    async def scan_site_firecrawl(self, scan: SiteScan) -> SiteScan:
        """Second phase for a site whose free scan returned "needs_firecrawl". Never raises."""
        site = scan.site
        try:
            state = self._site_states.setdefault(site["url"], {})
            return await self._scan_with_firecrawl(site, state, [scan.detail or ""])
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error scanning %s with Firecrawl", site["name"])
            return SiteScan(site=site, status="failed", detail=f"Unexpected error: {exc}")

    async def _scan_site_free(self, site: dict[str, str]) -> SiteScan:
        state = self._site_states.setdefault(site["url"], {})
        reasons: list[str] = []

        # 1. Feed
        if site.get("feed"):
            scan = await self._scan_feed(site)
            if scan is not None:
                return scan
            reasons.append("feed unavailable")

        # 2. Direct fetch. Skipped for a while only for sites known to be JavaScript shells
        #    (that attempt would waste an LLM call); blocked sites are always retried
        #    because a blocked request is free and some bot filters pass intermittently.
        if self._should_try_direct(state):
            result = await self._http.fetch(site["url"])
            usable, why = self._usable_listing(result)
            firecrawl_reason = "blocked"
            if usable and result is not None:
                firecrawl_reason = "javascript"
                digest = build_link_digest(result.content, result.final_url)
                if len(digest.links) >= _MIN_LISTING_LINKS:
                    scan = await self._scan_digest(site, state, digest, result.final_url, "direct")
                    if scan is not None:
                        return scan
                    reasons.append("no articles in direct HTML (JavaScript-rendered?)")
                else:
                    reasons.append(f"only {len(digest.links)} links in direct HTML")
            else:
                reasons.append(why)
            state["firecrawl_reason"] = firecrawl_reason
            if state.get("method") == "firecrawl":
                # Periodic re-check done and the site still needs Firecrawl.
                state["method_checked"] = self._today.isoformat()

        # 3. Firecrawl fallback happens in the second phase (see scan_sites).
        why = "; ".join(r for r in reasons if r) or "direct fetch skipped (site needs Firecrawl)"
        return SiteScan(site=site, status="needs_firecrawl", detail=why)

    def _should_try_direct(self, state: dict[str, Any]) -> bool:
        if state.get("method") != "firecrawl" or state.get("firecrawl_reason") != "javascript":
            return True
        checked = state.get("method_checked")
        if not checked:
            return True
        try:
            checked_date = date.fromisoformat(checked)
        except ValueError:
            return True
        return (self._today - checked_date).days >= self._settings.direct_recheck_days

    @staticmethod
    def _usable_listing(result: FetchResult | None) -> tuple[bool, str]:
        if result is None:
            return False, "network error"
        if result.is_challenge:
            return False, "bot challenge page"
        if not result.ok:
            return False, f"HTTP {result.status}"
        if not result.is_html:
            return False, f"unexpected content type {result.content_type}"
        return True, ""

    async def _scan_feed(self, site: dict[str, str]) -> SiteScan | None:
        result = await self._http.fetch(site["feed"])
        if result is None or not result.ok:
            logger.warning(
                "%s: feed fetch failed (%s); falling back to the listing page.",
                site["name"],
                result.status if result else "network error",
            )
            return None
        items = parse_feed(result.content, result.final_url)
        if not items:
            logger.warning("%s: feed has no items; falling back to the listing page.", site["name"])
            return None
        articles = [
            ArticleListing(
                title=item.title,
                url=item.url,
                date=item.published.isoformat() if item.published else None,
                source_name=site["name"],
                source_url=site["url"],
            )
            for item in items
        ]
        logger.info("%s: %d items from feed.", site["name"], len(articles))
        return SiteScan(site=site, status="ok", method="feed", articles=articles)

    async def _scan_digest(
        self,
        site: dict[str, str],
        state: dict[str, Any],
        digest: LinkDigest,
        page_url: str,
        method: FetchMethod,
        follow_listing_link: bool = True,
    ) -> SiteScan | None:
        """Extract articles from a page digest. Returns None if no articles were found."""
        fingerprint = link_fingerprint(digest.link_keys)

        # Nothing new on the page since the last successful extraction: skip the LLM call.
        previous = set(state.get("last_links") or [])
        if (
            state.get("baselined")
            and state.get("method") == method
            and previous
            and set(fingerprint) <= previous
        ):
            logger.info("%s: no new links since last run (%s).", site["name"], method)
            return SiteScan(
                site=site,
                status="unchanged",
                method=method,
                firecrawl_used=method == "firecrawl",
            )

        try:
            async with self._llm_semaphore:
                articles, listing_url = await extract_listing(
                    self._llm, site["name"], site["url"], page_url, digest, self._today
                )
        except OpenAIClientError as exc:
            logger.warning("%s: listing extraction failed: %s", site["name"], exc)
            return None

        if not articles and listing_url and follow_listing_link:
            logger.info("%s: no articles on page; following listing link %s", site["name"], listing_url)
            return await self._scan_followed_listing(site, state, listing_url, method)

        if not articles:
            return None
        logger.info("%s: %d articles extracted (%s).", site["name"], len(articles), method)
        return SiteScan(
            site=site,
            status="ok",
            method=method,
            articles=articles,
            link_fingerprint=fingerprint,
            firecrawl_used=method == "firecrawl",
        )

    async def _scan_followed_listing(
        self, site: dict[str, str], state: dict[str, Any], url: str, method: FetchMethod
    ) -> SiteScan | None:
        if method == "direct":
            result = await self._http.fetch(url)
            usable, _ = self._usable_listing(result)
            if not usable or result is None:
                return None
            digest = build_link_digest(result.content, result.final_url)
            page_url = result.final_url
        else:
            if self._firecrawl is None or not self._budget.try_spend():
                return None
            try:
                html = await self._firecrawl.scrape_html(url)
            except FirecrawlClientError as exc:
                logger.warning("%s: %s", site["name"], exc)
                return None
            digest = build_link_digest(html, url)
            page_url = url
        return await self._scan_digest(
            site, state, digest, page_url, method, follow_listing_link=False
        )

    async def _scan_with_firecrawl(
        self, site: dict[str, str], state: dict[str, Any], reasons: list[str]
    ) -> SiteScan:
        why = "; ".join(r for r in reasons if r) or "direct fetch skipped"
        if self._firecrawl is None:
            return SiteScan(site=site, status="failed", detail=f"{why}; Firecrawl disabled")

        last = state.get("last_firecrawl_at")
        if state.get("method") == "firecrawl" and last:
            try:
                elapsed = self._now - datetime.fromisoformat(last)
            except ValueError:
                elapsed = timedelta.max
            cooldown = timedelta(hours=self._settings.firecrawl_listing_cooldown_hours)
            if elapsed < cooldown:
                hours = elapsed.total_seconds() / 3600
                return SiteScan(
                    site=site,
                    status="skipped",
                    method="firecrawl",
                    detail=f"Firecrawl site checked {hours:.1f}h ago (cooldown "
                    f"{self._settings.firecrawl_listing_cooldown_hours:g}h) - saves credits",
                )

        if not self._budget.try_spend():
            return SiteScan(
                site=site,
                status="skipped",
                method="firecrawl",
                detail=f"{why}; Firecrawl credit allowance for this run is used up",
            )

        try:
            html = await self._firecrawl.scrape_html(site["url"])
        except FirecrawlClientError as exc:
            logger.warning("%s: %s", site["name"], exc)
            return SiteScan(
                site=site, status="failed", detail=f"{why}; Firecrawl failed", firecrawl_used=True
            )

        digest = build_link_digest(html, site["url"])
        scan = await self._scan_digest(site, state, digest, site["url"], "firecrawl")
        if scan is not None:
            return scan
        return SiteScan(
            site=site,
            status="failed",
            detail=f"{why}; no articles found via Firecrawl either",
            firecrawl_used=True,
        )

    # ------------------------------------------------------------------
    # Article pages
    # ------------------------------------------------------------------

    async def fetch_article(
        self, listing: ArticleListing
    ) -> tuple[ArticlePage | None, FetchMethod | None, str | None]:
        """Fetch and parse an article page.

        Returns:
            (page, method, problem). page is None when no content is available; problem
            then explains why (it is written to the run report). Never raises.
        """
        if listing.url.lower().split("?")[0].endswith(".pdf"):
            return None, None, "article is a PDF document (title + link only)"

        page: ArticlePage | None = None
        problem: str | None = None
        try:
            result = await self._http.fetch(listing.url, referer=listing.source_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Direct fetch error for %s: %s", listing.url, exc)
            result = None

        if result is None:
            problem = "network error"
        elif result.is_challenge or (not result.ok and result.status in (401, 403, 429, 503)):
            problem = f"blocked by the site (HTTP {result.status})"
        elif not result.ok:
            problem = f"HTTP {result.status}"
        elif not result.is_html:
            return None, None, f"not an HTML page ({result.content_type})"
        else:
            page = extract_article_page(result.content, result.final_url)
            if len(page.text) >= _MIN_ARTICLE_CHARS:
                return page, "direct", None
            problem = "article body is rendered by JavaScript"

        firecrawl_page, firecrawl_problem = await self.fetch_article_firecrawl(listing)
        if firecrawl_page is not None and len(firecrawl_page.text) > len(page.text if page else ""):
            return firecrawl_page, "firecrawl", None
        if page is not None and page.text:
            return page, "direct", None
        return None, None, f"{problem}; {firecrawl_problem}" if firecrawl_problem else problem

    async def fetch_article_firecrawl(
        self, listing: ArticleListing
    ) -> tuple[ArticlePage | None, str | None]:
        """Render an article with Firecrawl if the credit allowance permits. Never raises.

        Returns:
            (page, problem)
        """
        if self._firecrawl is None:
            return None, "Firecrawl disabled"
        if not self._budget.try_spend():
            return None, "no Firecrawl credits left in this run's allowance"
        try:
            html = await self._firecrawl.scrape_html(listing.url)
            return extract_article_page(html, listing.url), None
        except FirecrawlClientError as exc:
            logger.warning("Article %s: %s", listing.url, exc)
            return None, "Firecrawl failed"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Article %s: unexpected parse error: %s", listing.url, exc)
            return None, f"parse error: {exc}"
