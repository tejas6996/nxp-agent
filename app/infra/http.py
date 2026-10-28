"""
Direct HTTP infrastructure client.

Fetches pages without Firecrawl (free) using curl_cffi, which reproduces real browser
TLS/HTTP2 fingerprints. Many newsrooms that block plain Python clients serve normal
HTML to these requests. When one fingerprint is challenged, the next one is tried.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from app.infra.url_guard import UnsafeURLError, curl_resolve_entry, resolve_public
from app.settings import Settings

logger = logging.getLogger(__name__)

# Browser fingerprints tried in order when a site blocks or challenges a request.
_IMPERSONATIONS = ("chrome", "safari", "firefox")
_ROUNDS = 2
_ROUND_PAUSE_SECONDS = 3.0

# Statuses that usually mean "bot blocked" and are worth retrying with another fingerprint.
_BLOCK_STATUSES = frozenset({401, 403, 406, 429, 503})

# <title> markers of interstitial bot-check pages (often served with HTTP 200).
_CHALLENGE_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "challenge",
    "access denied",
    "checking your browser",
    "security check",
    "are you a robot",
    "captcha",
    "request unsuccessful",
)
# Real pages are large; bot-check interstitials are small. Both conditions must hold so a
# genuine article titled e.g. "... Innovation Challenge" is never mistaken for a block page.
_CHALLENGE_MAX_BYTES = 50_000
_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

_MAX_BYTES = 8 * 1024 * 1024
# curl refuses bodies above this outright (pages are cut to _MAX_BYTES anyway).
_MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
_MAX_REDIRECTS = 6
_QUICK_TIMEOUT_SECONDS = 10
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass
class FetchResult:
    """Outcome of a direct page fetch."""

    url: str
    final_url: str
    status: int
    content: bytes
    content_type: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.content)

    @property
    def is_html(self) -> bool:
        ct = self.content_type.lower()
        return "html" in ct or "xml" in ct or ct == ""

    @property
    def is_challenge(self) -> bool:
        if len(self.content) > _CHALLENGE_MAX_BYTES:
            return False
        match = _TITLE_RE.search(self.content)
        title = match.group(1).decode("utf-8", errors="ignore").lower() if match else ""
        return any(marker in title for marker in _CHALLENGE_TITLE_MARKERS)


def _strip_default_port(url: str) -> str:
    """https://host:443/x -> https://host/x (keeps stored article links clean)."""
    parts = urlparse(url)
    if (parts.scheme == "https" and parts.port == 443) or (parts.scheme == "http" and parts.port == 80):
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        return parts._replace(netloc=host).geturl()
    return url


class HttpFetcher:
    """Async page fetcher with browser impersonation and per-host politeness limits."""

    def __init__(self, settings: Settings) -> None:
        self._timeout = settings.http_timeout_seconds
        self._global = asyncio.Semaphore(settings.http_max_concurrent)
        self._max_per_host = settings.http_max_per_host
        self._host_limits: dict[str, asyncio.Semaphore] = {}
        self._session: AsyncSession | None = None
        # Cookies per website for the life of this fetcher (one run). Bot filters often
        # set a cookie on the first, blocked request and accept the retry that sends it.
        self._cookies: dict[str, dict[str, str]] = {}

    async def __aenter__(self) -> "HttpFetcher":
        self._session = AsyncSession(max_clients=32)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _host_limit(self, url: str) -> asyncio.Semaphore:
        host = urlparse(url).netloc.lower()
        if host not in self._host_limits:
            self._host_limits[host] = asyncio.Semaphore(self._max_per_host)
        return self._host_limits[host]

    async def fetch(
        self, url: str, referer: str | None = None, quick: bool = False, accept: str | None = None
    ) -> FetchResult | None:
        """Fetch a URL, rotating browser fingerprints when blocked.

        Returns:
            The best FetchResult obtained (check `.ok` / `.is_challenge`), or None if
            every attempt failed at the network level.
        """
        assert self._session is not None, "HttpFetcher must be used as an async context manager"
        last: FetchResult | None = None
        verify = True
        network_errors = 0
        # quick=True: one attempt with a short timeout (used to probe guessed feed URLs,
        # where a block or 404 simply means "not this one").
        attempts = [_IMPERSONATIONS[0]] if quick else [imp for _ in range(_ROUNDS) for imp in _IMPERSONATIONS]
        timeout = min(self._timeout, _QUICK_TIMEOUT_SECONDS) if quick else self._timeout
        async with self._global, self._host_limit(url):
            try:
                # Refuse non-public addresses before any request is made.
                await resolve_public(url)
            except UnsafeURLError as exc:
                logger.warning("Not fetching %s: %s", url, exc)
                return None
            for attempt, impersonate in enumerate(attempts):
                if attempt and attempt % len(_IMPERSONATIONS) == 0:
                    # Some bot filters (e.g. Keysight) pass requests intermittently; pause
                    # before a second round of fingerprints.
                    await asyncio.sleep(_ROUND_PAUSE_SECONDS)
                try:
                    result = await self._get(url, impersonate, verify, referer, timeout, accept)
                except UnsafeURLError as exc:
                    # A redirect pointed somewhere that is not the public internet.
                    logger.warning("Stopped fetching %s: %s", url, exc)
                    return None
                except RequestException as exc:
                    message = str(exc)
                    # Some sites (e.g. Andes) serve an incomplete certificate chain.
                    # These are public news pages, so retry once without verification.
                    if verify and ("SSL" in message or "certificate" in message):
                        logger.debug("TLS verification failed for %s; retrying unverified.", url)
                        verify = False
                        try:
                            result = await self._get(url, impersonate, verify, referer, timeout, accept)
                        except RequestException as exc2:
                            logger.debug("Fetch failed for %s (%s): %s", url, impersonate, exc2)
                            continue
                    else:
                        logger.debug("Fetch failed for %s (%s): %s", url, impersonate, exc)
                        network_errors += 1
                        # DNS failures are permanent; repeated timeouts mean the host is
                        # down - don't spend minutes cycling through fingerprints.
                        if "resolve host" in message.lower() or network_errors >= 2:
                            return last
                        continue

                last = result
                if result.ok and not result.is_challenge:
                    return result
                if result.status not in _BLOCK_STATUSES and not result.is_challenge:
                    # 404/410/500 etc. will not change with another fingerprint.
                    return result
                logger.debug(
                    "Blocked fetching %s with %s (status %s); trying next fingerprint.",
                    url,
                    impersonate,
                    result.status,
                )
        return last

    async def _get(
        self,
        url: str,
        impersonate: str,
        verify: bool,
        referer: str | None,
        timeout: float | None = None,
        accept: str | None = None,
    ) -> FetchResult:
        """One request, following redirects by hand: every hop is checked by the URL
        guard and the connection is pinned to the address that was checked, so
        neither a redirect nor a changed DNS answer can reach a non-public address."""
        headers = {"Accept-Language": "en-US,en;q=0.9"}
        if referer:
            headers["Referer"] = referer
        if accept:
            # e.g. image downloads ask for formats the image pipeline can read, so a CDN
            # that negotiates by Accept sends JPEG/PNG/WebP rather than AVIF/JPEG XL.
            headers["Accept"] = accept
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            host, port, ip = await resolve_public(current)
            cookies = self._cookies.setdefault(host, {})
            options = {
                CurlOpt.RESOLVE: [curl_resolve_entry(host, port, ip)],
                CurlOpt.PROTOCOLS_STR: "http,https",
                CurlOpt.REDIR_PROTOCOLS_STR: "http,https",
                CurlOpt.MAXFILESIZE: _MAX_DOWNLOAD_BYTES,
            }
            async with AsyncSession(curl_options=options) as session:
                response = await session.get(
                    current,
                    impersonate=impersonate,  # type: ignore[arg-type]
                    timeout=timeout or self._timeout,
                    allow_redirects=False,
                    verify=verify,
                    headers=headers,
                    cookies=cookies or None,
                )
            try:
                cookies.update({k: v for k, v in response.cookies.items() if isinstance(v, str)})
            except Exception:  # noqa: BLE001 - cookies are a nicety, never a failure
                pass
            location = response.headers.get("location")
            if response.status_code in _REDIRECT_STATUSES and location:
                current = _strip_default_port(urljoin(current, location))
                continue
            return FetchResult(
                url=url,
                final_url=current,
                status=response.status_code,
                content=response.content[:_MAX_BYTES],
                content_type=response.headers.get("content-type", "") or "",
            )
        raise RequestException(f"Too many redirects for {url}")
