"""
Firecrawl infrastructure client.

Firecrawl is only a fallback for pages that cannot be fetched directly (JavaScript-only
pages or hard bot protection). Every call costs credits, so this module also provides
FirecrawlBudget, which caps spending per run based on the live credit balance so the
free plan (1,000 credits/month) is never exhausted before the billing period resets.
"""

import asyncio
import logging
import math
import time
import warnings
from datetime import datetime, timezone

# The firecrawl SDK triggers harmless pydantic "shadows an attribute" warnings on import.
warnings.filterwarnings("ignore", message=r'Field name "json"', category=UserWarning)

from firecrawl import Firecrawl  # noqa: E402

from app.exceptions import FirecrawlClientError  # noqa: E402
from app.settings import Settings  # noqa: E402

logger = logging.getLogger(__name__)

# Free plan: 10 scrapes/minute. Space calls slightly wider than that to stay clear of 429s.
_MIN_SECONDS_BETWEEN_CALLS = 6.5


class FirecrawlClient:
    """Async-friendly wrapper around the Firecrawl SDK."""

    def __init__(self, settings: Settings) -> None:
        self._client = Firecrawl(api_key=settings.firecrawl_api_key)
        self._proxy = settings.firecrawl_proxy
        self._max_age_ms = settings.firecrawl_max_age_minutes * 60 * 1000
        self._semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_requests))
        self._rate_lock = asyncio.Lock()
        self._last_call = 0.0

    async def _respect_rate_limit(self) -> None:
        async with self._rate_lock:
            wait = self._last_call + _MIN_SECONDS_BETWEEN_CALLS - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def scrape_html(self, url: str) -> str:
        """Render a URL with Firecrawl and return the page's raw HTML.

        Raw HTML (rather than markdown) is requested so Firecrawl pages go through
        exactly the same parsing as directly fetched pages. Costs 1 credit with the
        basic proxy.

        Raises:
            FirecrawlClientError: If the Firecrawl API returns an error or no content.
        """
        async with self._semaphore:
            await self._respect_rate_limit()
            try:
                logger.info("Firecrawl scrape (1 credit): %s", url)
                document = await asyncio.to_thread(
                    self._client.scrape,
                    url,
                    formats=["rawHtml"],
                    only_main_content=False,
                    max_age=self._max_age_ms,
                    proxy=self._proxy,
                    block_ads=True,
                    remove_base64_images=True,
                    timeout=60000,
                )
            except Exception as exc:
                raise FirecrawlClientError(f"Firecrawl scrape failed for {url}: {exc}") from exc
        html = getattr(document, "raw_html", None) or getattr(document, "html", None) or ""
        if not html:
            raise FirecrawlClientError(f"Firecrawl returned no HTML for {url}")
        return html

    async def remaining_credits(self) -> tuple[int, datetime | None] | None:
        """Return (remaining_credits, billing_period_end), or None if unavailable.

        This endpoint does not consume credits.
        """
        try:
            usage = await asyncio.to_thread(self._client.get_credit_usage)
        except Exception as exc:
            logger.warning("Could not read Firecrawl credit balance: %s", exc)
            return None
        period_end: datetime | None = None
        raw_end = getattr(usage, "billing_period_end", None)
        if raw_end:
            try:
                period_end = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
            except ValueError:
                period_end = None
        return int(usage.remaining_credits), period_end


class FirecrawlBudget:
    """Tracks how many Firecrawl credits this run may spend.

    The per-run allowance spreads the remaining balance evenly over the runs left in the
    billing period (runs_per_day x days_left), never dips into the reserve, and never
    exceeds the configured per-run cap.
    """

    def __init__(self, allowance: int) -> None:
        self.allowance = max(0, allowance)
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.allowance - self.used

    def try_spend(self, credits: int = 1) -> bool:
        if self.used + credits > self.allowance:
            return False
        self.used += credits
        return True

    @staticmethod
    def compute_allowance(
        remaining_credits: int | None,
        period_end: datetime | None,
        settings: Settings,
        now: datetime | None = None,
    ) -> int:
        cap = settings.firecrawl_max_credits_per_run
        if remaining_credits is None:
            # Balance unknown: stay conservative rather than risk overspending.
            return min(cap, 5)
        spendable = remaining_credits - settings.firecrawl_credit_reserve
        if spendable <= 0:
            return 0
        now = now or datetime.now(timezone.utc)
        days_left = 30.0
        if period_end is not None:
            days_left = max((period_end - now).total_seconds() / 86400, 0.25)
        runs_left = max(1, math.ceil(days_left * max(1, settings.firecrawl_runs_per_day)))
        return max(0, min(cap, spendable // runs_left))

    @classmethod
    async def create(cls, client: FirecrawlClient | None, settings: Settings) -> "FirecrawlBudget":
        if client is None:
            return cls(0)
        balance = await client.remaining_credits()
        remaining, period_end = balance if balance else (None, None)
        allowance = cls.compute_allowance(remaining, period_end, settings)
        logger.info(
            "Firecrawl credits remaining: %s (period ends %s). Allowance for this run: %d.",
            remaining if remaining is not None else "unknown",
            period_end.date().isoformat() if period_end else "unknown",
            allowance,
        )
        return cls(allowance)
