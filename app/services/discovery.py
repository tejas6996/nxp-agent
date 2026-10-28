"""
Find the cheapest way to read a company newsroom (used by the admin "Add newsroom"
check and by scripts/discover_sites.py for bulk lists).

Order: RSS/Atom feed (free, no AI) -> the listing page itself (free fetch + one small
AI call to pick out the articles) -> "needs_firecrawl" (nothing is spent here).
Every fetch goes through HttpFetcher, i.e. through the URL guard (public internet only).
"""

import asyncio
import re
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.exceptions import OpenAIClientError
from app.infra.http import FetchResult, HttpFetcher
from app.infra.openai import OpenAIClient
from app.services.agent import extract_listing
from app.services.parsing import build_link_digest, parse_date, parse_feed, url_key

MIN_LISTING_LINKS = 5
MIN_FEED_ITEMS = 2
FRESH_DAYS = 365  # newest item older than this -> "stale" (company stopped posting)

_NEWS_LINK_RE = re.compile(
    r"press[\s_-]*releases?|news[\s_-]*releases?|newsroom|press[\s_-]*room|press[\s_-]*center|"
    r"media[\s_-]*center|\bnews\b|\bpress\b|\bmedia\b",
    re.I,
)
_FEED_HREF_RE = re.compile(r"(rss|atom|/feed/?$|/feed/?\?|\.xml($|\?))", re.I)


def _host(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _same_site(a: str, b: str) -> bool:
    ha, hb = _host(a), _host(b)
    if not ha or not hb:
        return False
    base = lambda h: ".".join(h.split(".")[-2:])  # noqa: E731
    return ha == hb or base(ha) == base(hb)


def _usable(result: FetchResult | None) -> str:
    """'' when the page is usable HTML, else the reason."""
    if result is None:
        return "network error / DNS"
    if result.is_challenge:
        return "bot challenge page"
    if not result.ok:
        return f"HTTP {result.status}"
    if not result.is_html:
        return f"not HTML ({result.content_type})"
    return ""


def _is_homepage(url: str) -> bool:
    return urlparse(url).path.strip("/") == "" and not urlparse(url).query


def _site_name(html: bytes, url: str) -> str:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        return _host(url)
    meta = soup.find("meta", attrs={"property": "og:site_name"})
    if meta and meta.get("content"):
        return meta["content"].strip()[:80]
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    parts = [p.strip() for p in re.split(r"\s[|\-–—:]\s", title) if p.strip()]
    if parts:
        return min(parts, key=len)[:80] if len(parts) > 1 else parts[0][:80]
    return _host(url)


def _specificity(feed_url: str, page_url: str) -> int:
    """How many leading path segments the feed shares with the listing page."""
    if not _same_site(feed_url, page_url):
        return -1
    a = [p for p in urlparse(feed_url).path.lower().split("/") if p]
    b = [p for p in urlparse(page_url).path.lower().split("/") if p]
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


_PARKED_RE = re.compile(r"domain (is )?for sale|buy this domain|parked (free|domain)|domain has expired|"
                        r"this domain may be for sale|sedoparking|hugedomains|godaddy\.com/domain", re.I)
_APP_SHELL_RE = re.compile(r"__NEXT_DATA__|id=[\"']root[\"']|id=[\"']app[\"']|ng-app|data-reactroot|"
                           r"window\.__NUXT__|<script[^>]+src=[^>]+(bundle|main|app|chunk)[^>]*\.js", re.I)


def _dead_page(html: bytes) -> str:
    """Reason when the page is a parked, expired or empty domain (not a JavaScript app)."""
    text = html.decode("utf-8", errors="ignore") if isinstance(html, bytes) else str(html)
    no_links = not build_link_digest(html, "https://example.invalid/").links
    if _PARKED_RE.search(text) or (no_links and re.search(r"adsense|adsbygoogle|caf\.js", text, re.I)):
        return "parked / for-sale domain"
    if no_links and not _APP_SHELL_RE.search(text):
        return "empty page (site no longer maintained)"
    return ""


def _feed_candidates(html: bytes, page_url: str) -> list[str]:
    """Feed URLs advertised by the page plus the usual guesses, in priority order."""
    out: list[str] = []

    def add(u: str) -> None:
        u = urljoin(page_url, u.strip())
        if u.startswith("http") and u not in out and "comments" not in u.lower():
            out.append(u)

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        soup = None
    if soup is not None:
        for link in soup.find_all("link", attrs={"rel": True, "href": True}):
            rel = " ".join(link.get("rel") or []).lower()
            kind = (link.get("type") or "").lower()
            if "alternate" in rel and ("rss" in kind or "atom" in kind or "xml" in kind):
                add(link["href"])
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if _FEED_HREF_RE.search(href) and _same_site(urljoin(page_url, href), page_url):
                add(href)

    text = html.decode("utf-8", errors="ignore").lower() if isinstance(html, bytes) else str(html).lower()
    parsed = urlparse(page_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    page = page_url.split("#")[0].split("?")[0].rstrip("/")
    if "wp-content" in text or "wordpress" in text:
        add(page + "/feed/")
        add(root + "/feed/")
    if "q4cdn" in text or "q4inc" in text or "gcs-web" in text or "/news-releases" in page:
        add(root + "/rss/news-releases.xml")
        add(root + "/rss/pressrelease.aspx")
    if "drupal" in text:
        add(root + "/rss.xml")
    for guess in ("/feed", "/rss", "/rss.xml", "/feed.xml", "/index.xml"):
        add(page + guess)
    return out[:14]


async def _try_feed(http: HttpFetcher, url: str, today: date) -> dict[str, Any] | None:
    result = await http.fetch(url, quick=True)
    if result is None or not result.ok:
        return None
    items = parse_feed(result.content, result.final_url)
    if len(items) < MIN_FEED_ITEMS:
        return None
    dated = [i.published for i in items if i.published]
    newest = max(dated) if dated else None
    return {
        "feed": result.final_url if url_key(result.final_url) != url_key(url) else url,
        "item_keys": [url_key(i.url) for i in items[:10]],
        "items": len(items),
        "newest": newest.isoformat() if newest else None,
        "titles": [i.title for i in items[:3]],
        "samples": [{"title": i.title, "url": i.url, "date": i.published.isoformat() if i.published else None}
                    for i in items[:3]],
        "age_days": (today - newest).days if newest else None,
    }


def _news_links(html: bytes, page_url: str) -> list[str]:
    """Links on a (home) page that look like the newsroom, best first."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        return []
    scored: list[tuple[int, str]] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"]).split("#")[0]
        if not href.startswith("http") or not _same_site(href, page_url):
            continue
        label = f"{a.get_text(' ', strip=True)} {href}"
        if not _NEWS_LINK_RE.search(label):
            continue
        score = 0
        low = label.lower()
        if "press release" in low or "press-release" in low or "pressrelease" in low:
            score += 6
        if "newsroom" in low or "news-room" in low or "press room" in low:
            score += 4
        if "news" in low:
            score += 2
        if any(w in low for w in ("event", "blog", "career", "webinar", "newsletter", "subscribe")):
            score -= 3
        scored.append((score, href))
    seen: set[str] = set()
    out = []
    for _, href in sorted(scored, key=lambda x: -x[0]):
        k = url_key(href)
        if k not in seen:
            seen.add(k)
            out.append(href)
    return out[:3]


async def _direct(
    http: HttpFetcher, llm: OpenAIClient, sem: asyncio.Semaphore, url: str, html: bytes,
    final_url: str, today: date,
) -> dict[str, Any]:
    digest = build_link_digest(html, final_url)
    if len(digest.links) < MIN_LISTING_LINKS:
        return {"ok": False, "why": f"only {len(digest.links)} links in the HTML (JavaScript page?)"}
    try:
        async with sem:
            articles, listing_url = await extract_listing(llm, _host(final_url), url, final_url, digest, today)
    except OpenAIClientError as exc:
        return {"ok": False, "why": f"AI listing extraction failed: {exc}"}
    if not articles and listing_url:
        result = await http.fetch(listing_url)
        if not _usable(result) and result is not None:
            digest = build_link_digest(result.content, result.final_url)
            try:
                async with sem:
                    articles, _ = await extract_listing(llm, _host(listing_url), listing_url, result.final_url, digest, today)
            except OpenAIClientError:
                articles = []
            if articles:
                final_url = listing_url
    if not articles:
        return {"ok": False, "why": "no articles found in the HTML (JavaScript page?)"}
    dates = [d for d in (parse_date(a.date) for a in articles) if d]
    newest = max(dates) if dates else None
    return {
        "ok": True,
        "listing": final_url,
        "items": len(articles),
        "newest": newest.isoformat() if newest else None,
        "age_days": (today - newest).days if newest else None,
        "titles": [a.title for a in articles[:3]],
        "samples": [{"title": a.title, "url": a.url, "date": a.date} for a in articles[:3]],
    }


async def check_url(
    http: HttpFetcher, llm: OpenAIClient, sem: asyncio.Semaphore, url: str, today: date
) -> dict[str, Any]:
    entry: dict[str, Any] = {"input": url, "notes": []}
    fetch_url = url.split("#")[0]
    result = await http.fetch(fetch_url)
    problem = _usable(result)
    if problem and fetch_url.startswith("http://"):
        alt = "https://" + fetch_url[len("http://"):]
        r2 = await http.fetch(alt)
        if not _usable(r2):
            result, problem = r2, ""
            entry["notes"].append("works over https")
    page_url = result.final_url if result is not None else fetch_url
    entry["http_status"] = result.status if result is not None else None
    entry["final_url"] = page_url

    # Moved / dead page: look for the newsroom from the home page.
    moved = (
        (problem and result is not None and result.status in (404, 410))
        or (not problem and _is_homepage(page_url) and not _is_homepage(fetch_url))
    )
    if moved or (problem and result is None):
        parsed = urlparse(page_url if result is not None else fetch_url)
        home = f"{parsed.scheme}://{parsed.netloc}/"
        home_res = await http.fetch(home)
        if _usable(home_res) or home_res is None:
            entry.update(verdict="dead", why=problem or "page moved and home page unreachable")
            return entry
        entry["name_guess"] = _site_name(home_res.content, home_res.final_url)
        for cand in _news_links(home_res.content, home_res.final_url):
            cres = await http.fetch(cand)
            if not _usable(cres) and cres is not None:
                entry["notes"].append(f"original page {problem or 'redirected to home'}; newsroom found at {cres.final_url}")
                result, problem, page_url = cres, "", cres.final_url
                entry["final_url"] = page_url
                break
        else:
            entry.update(verdict="dead", why=f"{problem or 'redirects to home page'}; no newsroom link found")
            return entry

    if problem:
        entry.update(verdict="needs_firecrawl" if "challenge" in problem or (result and result.status in (401, 403, 429, 503)) else "dead",
                     why=problem)
        return entry

    html = result.content
    entry.setdefault("name_guess", _site_name(html, page_url))
    dead = _dead_page(html)
    if dead:
        entry.update(verdict="dead", why=dead)
        return entry

    # Feeds first (free, no AI). Among fresh feeds, prefer the one closest to the
    # listing page (a category feed over the whole-site feed), then the freshest.
    # A feed is only used when it carries the same items as the listing page: most of
    # its latest items must be links on that page (a WordPress site-wide feed mixes
    # blog posts in), or the feed must live at/under the listing page's path.
    page_keys = {url_key(link.url) for link in build_link_digest(html, page_url).links.values()}
    depth = len([p for p in urlparse(page_url).path.split("/") if p])
    best: dict[str, Any] | None = None
    fresh: list[dict[str, Any]] = []
    site_wide: dict[str, Any] | None = None
    candidates = _feed_candidates(html, page_url)
    # Probe the guesses together (quick single requests; per-site limits still apply).
    probed = await asyncio.gather(*(_try_feed(http, cand, today) for cand in candidates))
    for feed in probed:
        if not feed:
            continue
        keys = feed.pop("item_keys")
        feed["overlap"] = round(sum(1 for k in keys if k in page_keys) / max(1, len(keys)), 2)
        feed["specificity"] = _specificity(feed["feed"], page_url)
        matches = feed["overlap"] >= 0.7 or (feed["specificity"] >= depth and depth > 0)
        if feed["age_days"] is not None and feed["age_days"] <= FRESH_DAYS:
            if matches:
                fresh.append(feed)
            elif site_wide is None:
                site_wide = feed
        elif best is None:
            best = feed
    if fresh:
        chosen = max(fresh, key=lambda f: (f["overlap"], f["specificity"], -(f["age_days"] or 0)))
        entry.update(verdict="feed", method="feed", listing=page_url, **chosen)
        return entry

    direct = await _direct(http, llm, sem, url, html, page_url, today)
    if direct["ok"]:
        stale = direct["age_days"] is not None and direct["age_days"] > FRESH_DAYS
        entry.update(verdict="stale" if stale else "direct", method="direct",
                     **{k: v for k, v in direct.items() if k != "ok"})
        if best is not None:
            entry["notes"].append(f"feed {best['feed']} found but its newest item is {best['newest']}")
        return entry
    if best is not None:
        entry.update(verdict="stale", method="feed", listing=page_url, **best)
        return entry
    if site_wide is not None:
        entry.update(verdict="feed", method="feed", listing=page_url, **site_wide)
        entry["notes"].append("site-wide feed (the listing page itself could not be read); may include blog posts")
        return entry
    entry.update(verdict="needs_firecrawl", why=direct["why"])
    return entry


# ---------------------------------------------------------------------------
# Single link from the admin ("Add newsroom")
# ---------------------------------------------------------------------------

_LATIN_RE = re.compile(r"[A-Za-z]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
ADDABLE = ("feed", "direct", "stale")


def mostly_non_english(titles: list[str]) -> bool:
    """True when most letters in the sample headlines are not Latin (e.g. Chinese)."""
    text = " ".join(titles or [])
    letters = _LETTER_RE.findall(text)
    if len(letters) < 12:
        return False
    return len(_LATIN_RE.findall(text)) / len(letters) < 0.6


_GENERIC_NAME_RE = re.compile(
    r"^(the )?(latest |all )?(news|press|media|newsroom|blog|home|homepage|stories|updates|insights)"
    r"( (releases?|room|center|centre|& events|and events|& media|and media|archive|list))?$",
    re.I,
)


def suggest_name(name_guess: str, url: str) -> str:
    """A company-like name: the page's site name unless it is generic ("News Releases"),
    else the domain (e.g. "microchip.com")."""
    name = re.sub(r"\s+", " ", str(name_guess or "")).strip(" |-–—:")
    if name and not _GENERIC_NAME_RE.match(name) and len(name) <= 60:
        return name
    return _host(url)


def _feed_items(content: bytes, base_url: str) -> list:
    head = content[:600].lstrip().lower()
    if not (head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head or b"<rdf" in head):
        return []
    return parse_feed(content, base_url)


async def check_newsroom(
    http: HttpFetcher, llm: OpenAIClient, sem: asyncio.Semaphore, url: str, today: date
) -> dict[str, Any]:
    """Check one link an editor typed. Works with a newsroom page, a company home page
    (the newsroom link on it is found and tried) or an RSS/Atom feed link."""
    first = await http.fetch(url)

    # A feed link: use it as the feed; the listing page is the feed's own site link.
    if first is not None and first.ok:
        items = _feed_items(first.content, first.final_url)
        if len(items) >= MIN_FEED_ITEMS:
            dated = [i.published for i in items if i.published]
            newest = max(dated) if dated else None
            site_link = ""
            try:
                import feedparser  # already a dependency (used by parse_feed)
                site_link = (feedparser.parse(first.content).feed.get("link") or "").strip()
            except Exception:  # noqa: BLE001
                site_link = ""
            listing = site_link if site_link.startswith(("http://", "https://")) else first.final_url
            age = (today - newest).days if newest else None
            return {
                "input": url, "notes": ["the link is an RSS/Atom feed"],
                "verdict": "stale" if age is not None and age > FRESH_DAYS else "feed",
                "method": "feed", "feed": first.final_url, "listing": listing, "final_url": first.final_url,
                "items": len(items), "newest": newest.isoformat() if newest else None, "age_days": age,
                "titles": [i.title for i in items[:3]],
                "samples": [{"title": i.title, "url": i.url, "date": i.published.isoformat() if i.published else None}
                            for i in items[:3]],
                "name_guess": _host(listing),
            }

    # A company home page: try the newsroom it links to first.
    if first is not None and not _usable(first) and _is_homepage(first.final_url):
        for cand in _news_links(first.content, first.final_url):
            found = await check_url(http, llm, sem, cand, today)
            if found.get("verdict") in ("feed", "direct"):
                found["notes"].insert(0, f"home page given; newsroom found at {found.get('listing') or cand}")
                found["input"] = url
                found["name_guess"] = suggest_name(_site_name(first.content, first.final_url), first.final_url)
                return found

    found = await check_url(http, llm, sem, url, today)
    found["name_guess"] = suggest_name(found.get("name_guess", ""), found.get("listing") or found.get("final_url") or url)
    return found
