"""
Deterministic HTML / feed parsing helpers.

The LLM never sees or writes raw URLs. Listing pages are rendered into a compact text
"digest" in which every hyperlink is replaced by a numeric ID, e.g. `[Headline](#12)`.
The model answers with IDs, and the exact URL is looked up from the page itself, so
links in the digest are always real links that exist on the source page.
"""

import html as html_lib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
from bs4 import BeautifulSoup, NavigableString, Tag

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_TRACKING_PARAMS = frozenset(
    {"gclid", "fbclid", "mc_cid", "mc_eid", "hslang", "_hsenc", "_hsmi", "mkt_tok", "cmpid"}
)


def clean_url(url: str) -> str:
    """Return the URL without fragment and tracking parameters (utm_*, gclid, ...)."""
    parsed = urlparse(url.strip())
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    kept = [
        (k, v)
        for k, v in pairs
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    # Leave the query string byte-for-byte untouched unless something was removed.
    query = parsed.query if len(kept) == len(pairs) else urlencode(kept)
    netloc = parsed.netloc
    try:
        if parsed.port in (80, 443):
            netloc = parsed.hostname or netloc
    except ValueError:
        pass
    return urlunparse(parsed._replace(netloc=netloc, query=query, fragment=""))


def url_key(url: str) -> str:
    """Canonical key used to decide whether two URLs point to the same article.

    Ignores scheme, letter case of the host, default ports, a leading "www.",
    trailing slashes, fragments and tracking parameters.
    """
    parsed = urlparse(clean_url(url))
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{host}{path}{query}"


def _is_http_link(url: str) -> bool:
    return urlparse(url).scheme in ("http", "https")


_SOCIAL_HOSTS = (
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com",
    "youtu.be", "weibo.com", "t.me", "tiktok.com", "threads.net", "pinterest.com",
    "bilibili.com", "vimeo.com", "wa.me", "reddit.com",
)


def is_social_link(url: str) -> bool:
    """True for links to social networks / video platforms (never news articles here)."""
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _SOCIAL_HOSTS)


# ---------------------------------------------------------------------------
# Text / date helpers
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


_NORM_MAP = str.maketrans(
    {
        "’": "'", "‘": "'", "“": '"', "”": '"',
        "–": "-", "—": "-", "‑": "-", "‐": "-",
        " ": " ", "®": "", "™": "", "©": "",
    }
)


def normalize_for_match(text: str) -> str:
    """Loose normalisation used to check that a title really appears on a page."""
    text = unicodedata.normalize("NFKC", text).translate(_NORM_MAP)
    return collapse_ws(text).casefold()


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%B %d, %Y",
    "%b %d, %Y",
    "%b. %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%Y/%m/%d",
    "%Y.%m.%d",
)


def parse_date(value: Any) -> date | None:
    """Parse common date representations into a date. Returns None when unsure."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = collapse_ws(str(value))
    if not text:
        return None
    iso = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso).date()
    except ValueError:
        pass
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Listing pages -> link digest
# ---------------------------------------------------------------------------

_DROP_TAGS = (
    "script", "style", "noscript", "svg", "iframe", "template", "select", "option",
    "button", "input", "textarea", "canvas", "map", "object", "dialog",
)
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5",
        "h6", "tr", "table", "tbody", "thead", "header", "footer", "main", "aside", "figure",
        "figcaption", "dl", "dt", "dd", "blockquote", "br", "hr", "form", "fieldset", "address",
    }
)
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_BOILERPLATE_HINTS = ("cookie", "onetrust", "consent", "gdpr")
_MAX_DIGEST_CHARS = 80_000


@dataclass
class PageLink:
    """A unique link found on a listing page."""

    id: int
    url: str
    text: str


@dataclass
class LinkDigest:
    """Listing page rendered as text with numbered links."""

    text: str
    links: dict[int, PageLink] = field(default_factory=dict)
    title: str = ""

    @property
    def link_keys(self) -> set[str]:
        return {url_key(link.url) for link in self.links.values()}

    @property
    def plain_text(self) -> str:
        """Digest text with link markup removed (used for title verification)."""
        return re.sub(r"\]\(#\d+\)", "", self.text).replace("[", "")


def _make_soup(html: str | bytes) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _has_ancestor(tag: Tag, names: Iterable[str]) -> bool:
    names = set(names)
    parent = tag.parent
    while parent is not None:
        if getattr(parent, "name", None) in names:
            return True
        parent = parent.parent
    return False


def _strip_page_chrome(soup: BeautifulSoup) -> None:
    """Remove scripts, navigation, site header/footer and cookie banners in place."""
    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()
    for tag in soup.find_all("nav"):
        tag.decompose()
    for tag in soup.find_all(attrs={"role": ["navigation", "banner", "contentinfo", "search"]}):
        if not _has_ancestor(tag, ("article", "main", "li")):
            tag.decompose()
    # Page-level header/footer only. A <header> inside an <article> card often holds the
    # headline itself and must be kept.
    for tag in soup.find_all(["header", "footer"]):
        if not tag.decomposed and not _has_ancestor(tag, ("article", "main", "li")):
            tag.decompose()
    for tag in soup.find_all(True):
        if tag.decomposed or tag.name in ("html", "body"):
            continue
        marker = " ".join(
            [tag.get("id") or ""] + list(tag.get("class") or [])
        ).lower()
        if any(hint in marker for hint in _BOILERPLATE_HINTS):
            if len(tag.get_text(" ", strip=True)) < 3000:
                tag.decompose()


def _anchor_text(a: Tag) -> str:
    text = collapse_ws(a.get_text(" ", strip=True))
    if text:
        return text
    for attr in ("aria-label", "title"):
        if a.get(attr):
            return collapse_ws(str(a[attr]))
    img = a.find("img", alt=True)
    if img is not None and img.get("alt"):
        return collapse_ws(str(img["alt"]))
    return ""


def build_link_digest(html: str | bytes, base_url: str) -> LinkDigest:
    """Render a listing page as text where links appear as `[text](#id)`.

    The same URL always receives the same ID, so a card's image link, headline link and
    "Read more" link all collapse to one choice for the model.
    """
    soup = _make_soup(html)
    page_title = collapse_ws(soup.title.get_text()) if soup.title else ""
    base_tag = soup.find("base", href=True)
    if base_tag is not None:
        base_url = urljoin(base_url, str(base_tag["href"]))
    _strip_page_chrome(soup)

    links: dict[int, PageLink] = {}
    ids_by_key: dict[str, int] = {}
    page_key = url_key(base_url)
    parts: list[str] = []

    def link_id_for(href: str, text: str) -> int | None:
        absolute = urljoin(base_url, href.strip())
        if not _is_http_link(absolute) or is_social_link(absolute):
            return None
        absolute = clean_url(absolute)
        key = url_key(absolute)
        if key == page_key:
            return None
        if key not in ids_by_key:
            new_id = len(ids_by_key) + 1
            ids_by_key[key] = new_id
            links[new_id] = PageLink(id=new_id, url=absolute, text=text)
        elif len(text) > len(links[ids_by_key[key]].text):
            links[ids_by_key[key]].text = text
        return ids_by_key[key]

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if type(child) is NavigableString:  # skip comments / CDATA
                    text = str(child)
                    if text.strip():
                        parts.append(collapse_ws(text) + " ")
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name == "a" and child.get("href"):
                text = _anchor_text(child)
                lid = link_id_for(str(child["href"]), text) if text else None
                if lid is not None:
                    parts.append(f" [{text}](#{lid}) ")
                elif text:
                    parts.append(text + " ")
                continue
            if name == "img":
                continue
            if name == "time" and child.get("datetime"):
                text = collapse_ws(child.get_text(" ", strip=True))
                parts.append(f" {text} [{child['datetime']}] ")
                continue
            is_block = name in _BLOCK_TAGS
            if is_block:
                parts.append("\n")
                if name in _HEADING_TAGS:
                    parts.append("## ")
            walk(child)
            if is_block:
                parts.append("\n")

    root = soup.body or soup
    walk(root)

    lines = [collapse_ws(line) for line in "".join(parts).split("\n")]
    text = "\n".join(line for line in lines if line)
    if len(text) > _MAX_DIGEST_CHARS:
        text = text[:_MAX_DIGEST_CHARS]
        # Drop links that fell outside the truncated text.
        visible = {int(m) for m in re.findall(r"\]\(#(\d+)\)", text)}
        links = {k: v for k, v in links.items() if k in visible}
    return LinkDigest(text=text, links=links, title=page_title)


# ---------------------------------------------------------------------------
# Article pages
# ---------------------------------------------------------------------------

_ARTICLE_NOISE_HINTS = (
    "related", "share", "social", "newsletter", "breadcrumb", "subscribe", "cookie",
    "onetrust", "consent", "sidebar", "promo", "recommend",
)
_MAX_ARTICLE_CHARS = 20_000
_BAD_IMAGE_HINTS = ("logo", "icon", "sprite", "avatar", "placeholder", "fallback", "blank", "pixel")


@dataclass
class ImageRef:
    """An image referenced by an article page."""

    url: str
    alt: str = ""
    # "news" (picked with the news item), "social" (og/twitter/JSON-LD, chosen by the
    # publisher), "content" (inside the article body) or "embedded" (only in the page's
    # script data, matched to the headline)
    origin: str = "content"
    # A site-wide picture (default share image, brand card), not this article's own.
    generic: bool = False


@dataclass
class ArticlePage:
    """Deterministically extracted data from a single article page."""

    url: str
    h1: str = ""
    meta_title: str = ""
    published: date | None = None
    image_url: str | None = None
    text: str = ""
    images: list[ImageRef] = field(default_factory=list)


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        ) or soup.find("meta", attrs={"itemprop": name})
        if tag is not None and tag.get("content"):
            return collapse_ws(str(tag["content"]))
    return ""


def _json_ld_values(soup: BeautifulSoup, key: str) -> list[Any]:
    found: list[Any] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == key:
                    found.append(v)
                visit(v)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            visit(json.loads(script.string or script.get_text() or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return found


def _image_from_ld(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return _image_from_ld(value[0])
    if isinstance(value, dict):
        return str(value.get("url") or value.get("contentUrl") or "")
    return ""


# Folders that hold site-template graphics (theme art, header banners, menu tiles), not
# article media. Theme folders are matched as CMS theme paths: a media library may well
# have its own "themes" folder of real photos (ti.com/content/dam/.../images/themes/...).
_TEMPLATE_PATH_HINTS = (
    "/wp-content/themes/", "/themes/custom/", "/themes/contrib/", "/sites/all/themes/",
    "/templates/", "/theme/", "/homepage/", "/header/", "/footer/", "/layout/",
    "/megamenu/", "/flags/", "/favicon",
    # author photos, e.g. secure.gravatar.com/avatar/<hash>
    "gravatar.com", "/avatar/", "/avatars/",
)

# Words in a file name that mark a site-wide picture (default share card, brand art)
# rather than the article's own. Matched as whole words of the name, so "shareholder"
# or "socially" do not count.
_GENERIC_NAME_WORDS = frozenset({
    "default", "defaults", "share", "sharing", "social", "socialmedia", "brand", "branding",
    "generic", "general", "fallback", "placeholder", "topnav", "nav", "menu", "header",
    "masthead", "site", "website", "corporate",
})
# "og"/"ogp" alone does not make a picture generic: Apple names the article's own share
# image "...-lp.jpg.og.jpg". Only a name made of nothing but such filler words
# ("ogp-top.jpeg", "og-image.png") is a site default.
_SHARE_MARKERS = frozenset({"og", "ogp", "opengraph"})
_FILLER_WORDS = _SHARE_MARKERS | frozenset({
    "image", "images", "img", "top", "main", "thumb", "thumbnail", "card", "banner",
    "large", "big", "small", "jpg", "jpeg", "png", "webp",
})


def generic_image_name(url: str) -> bool:
    """True if the image's file name says it is a site-wide picture, not the article's."""
    name = urlparse(url).path.rsplit("/", 1)[-1].lower()
    stem = name.rsplit(".", 1)[0]
    words = set(re.split(r"[^a-z0-9]+", stem)) - {""}
    if words & _GENERIC_NAME_WORDS:
        return True
    meaningful = {w for w in words if not w.isdigit() and len(w) > 2}
    return bool(words & _SHARE_MARKERS) and meaningful <= _FILLER_WORDS


def good_image(url: str) -> bool:
    """True for URLs that can be article images (not logos, icons, avatars, theme art)."""
    return _good_image(url)


def _good_image(url: str, allow_template_path: bool = False) -> bool:
    lower = url.lower().split("?")[0]
    if not _is_http_link(url) or lower.endswith((".svg", ".gif", ".pdf")):
        return False
    if not allow_template_path and _template_path(url):
        return False
    name = lower.rsplit("/", 1)[-1]
    return not any(hint in name for hint in _BAD_IMAGE_HINTS)


def _template_path(url: str) -> bool:
    lower = url.lower().split("?")[0]
    return any(hint in lower for hint in _TEMPLATE_PATH_HINTS)


def _largest_from_srcset(srcset: str) -> str:
    """Pick the widest candidate from a srcset attribute ("a.jpg 400w, b.jpg 1200w")."""
    best, best_w = "", -1.0
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        width = 0.0
        if len(bits) > 1 and bits[1][:-1].replace(".", "", 1).isdigit():
            width = float(bits[1][:-1]) * (1 if bits[1].endswith("w") else 1000)
        if width >= best_w:
            best, best_w = bits[0], width
    return best


# Lazy-loading plugins keep the real file in data-* attributes (src is often a blank
# placeholder). The "full size" ones (WordPress, Jetpack, zoom galleries) come first.
_FULL_SIZE_ATTRS = ("data-orig-file", "data-large-file", "data-full-url", "data-hi-res-src", "data-zoom-image")
_SRCSET_ATTRS = ("srcset", "data-srcset", "data-lazy-srcset")
_LAZY_SRC_ATTRS = ("data-src", "data-lazy-src", "data-original")


def _srcset_of(img: Tag) -> str:
    srcset = next((str(img.get(a)) for a in _SRCSET_ATTRS if img.get(a)), "")
    if not srcset and img.parent is not None and img.parent.name == "picture":
        source = img.parent.find("source", srcset=True)
        srcset = str(source["srcset"]) if source is not None else ""
    return srcset


def _img_src(img: Tag) -> str:
    full = next((str(img.get(a)) for a in _FULL_SIZE_ATTRS if img.get(a)), "")
    srcset = _srcset_of(img)
    best = _largest_from_srcset(srcset) if srcset else ""
    lazy = next((str(img.get(a)) for a in _LAZY_SRC_ATTRS if img.get(a)), "")
    return str(full or best or lazy or img.get("src") or "")


def _declared_size(img: Tag, attr: str) -> int | None:
    match = re.match(r"\s*(\d+)", str(img.get(attr) or ""))
    return int(match.group(1)) if match else None


def _srcset_max_width(srcset: str) -> float:
    widths = [float(m) for m in re.findall(r"\s(\d+(?:\.\d+)?)w\b", " " + srcset)]
    return max(widths, default=0.0)


def _displayed_small(img: Tag) -> bool:
    """An icon/thumbnail by its declared size - unless a large file is on offer anyway
    (a 280px-wide thumbnail often has a 1200w entry in its srcset)."""
    width, height = _declared_size(img, "width"), _declared_size(img, "height")
    small = (width is not None and width < 300) or (height is not None and height < 150)
    return small and _srcset_max_width(_srcset_of(img)) < 600


# "photo-1024x576.jpg" (WordPress) or "photo_200px.png": the same picture at a reduced
# size. The name without the suffix is usually the full-size original.
_SIZE_SUFFIX = re.compile(
    r"(?:-\d{2,4}x\d{2,4}|_\d{2,4}px|\.tmb-[a-z0-9]+)(?=\.(?:jpe?g|png|webp|avif)$)", re.I
)


def _full_size_variant(url: str) -> str | None:
    parts = urlparse(url)
    path = _SIZE_SUFFIX.sub("", parts.path)
    return urlunparse(parts._replace(path=path, query="")) if path != parts.path else None


_IMAGE_FILE = re.compile(r"\.(?:jpe?g|png|webp|avif)(?:\?|$)", re.I)
# Image URLs inside script data (Next.js/React payloads, JSON). Escaped slashes and
# ampersands are undone before matching.
_IMAGE_URL_IN_TEXT = re.compile(
    r"""(?:https?:)?//[^\s"'<>()\\]+?\.(?:jpe?g|png|webp|avif)(?:\?[^\s"'<>()\\]*)?"""
    r"""|(?<![\w.])/[^\s"'<>()\\:]+?\.(?:jpe?g|png|webp|avif)(?:\?[^\s"'<>()\\]*)?""",
    re.I,
)
_HEADLINE_STOPWORDS = frozenset({
    "about", "after", "announces", "announced", "launch", "launches", "launched", "their",
    "these", "those", "which", "while", "with", "where", "would", "could", "should", "today",
    "first", "industry", "global", "company", "introduces", "introducing", "delivers",
    "unveils", "expands", "release", "releases", "solution", "solutions", "technology",
    "technologies", "series", "family", "products", "product", "systems", "devices",
})


def _headline_words(title: str, page_url: str) -> set[str]:
    """Distinctive words of the headline and URL slug (not the company's own name)."""
    host = (urlparse(page_url).hostname or "").lower()
    slug = urlparse(page_url).path.rsplit("/", 1)[-1]
    words = set(re.split(r"[^a-z0-9]+", f"{title} {slug}".lower()))
    return {w for w in words if len(w) >= 5 and w not in _HEADLINE_STOPWORDS and w not in host}


def headline_words(title: str, page_url: str) -> set[str]:
    return _headline_words(title, page_url)


def _unescape_script(text: str) -> str:
    return (
        text.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
        .replace("\\u0022", '"').replace('\\"', '"').replace("\\/", "/").replace("&amp;", "&")
    )


_IMG_MARKUP = re.compile(r"<img\b[^>]*>", re.I)
_ATTR = re.compile(r"""\b(src|data-src|alt)\s*=\s*"([^"]*)\"""", re.I)


def _embedded_images(
    raw_html: str, script_text: str, page_url: str, words: set[str], limit: int = 6
) -> list[tuple[str, str]]:
    """(url, alt) of images that appear only in the page's script data.

    Two kinds are trusted: <img> markup inside the scripts (headless CMSes such as
    Sitecore or Contentful ship the article body as an HTML string in JSON), and image
    URLs whose file name shares a distinctive word with the headline (so logos and site
    art never match).
    """
    found: list[tuple[str, str]] = []

    def add(url: str, alt: str) -> None:
        url = urljoin(page_url, url)
        if all(url != f for f, _ in found):
            found.append((url, alt))

    for markup in _IMG_MARKUP.findall(_unescape_script(script_text)):
        attrs = {k.lower(): v for k, v in _ATTR.findall(markup)}
        src = attrs.get("src") or attrs.get("data-src") or ""
        if src and not src.startswith("data:"):
            add(src, attrs.get("alt", ""))
        if len(found) >= limit:
            return found
    if words:
        for match in _IMAGE_URL_IN_TEXT.finditer(_unescape_script(raw_html)):
            url = urljoin(page_url, match.group(0))
            name = urlparse(url).path.rsplit("/", 1)[-1].lower()
            if any(word in name for word in words):
                add(url, "")
                if len(found) >= limit:
                    break
    return found


def _pick_main_root(soup: BeautifulSoup) -> Tag:
    articles = soup.find_all("article")
    if articles:
        best = max(articles, key=lambda t: len(t.get_text(" ", strip=True)))
        if len(best.get_text(" ", strip=True)) >= 400:
            return best
    for candidate in (soup.find(attrs={"role": "main"}), soup.find("main")):
        if candidate is not None and len(candidate.get_text(" ", strip=True)) >= 200:
            return candidate
    return soup.body or soup


def _render_text(root: Tag) -> str:
    parts: list[str] = []

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if type(child) is NavigableString and str(child).strip():
                    parts.append(collapse_ws(str(child)) + " ")
                continue
            if not isinstance(child, Tag):
                continue
            if child.name in ("td", "th"):
                # Keep spec tables readable: one row per line, cells separated by " | ".
                cell = collapse_ws(child.get_text(" ", strip=True))
                if cell:
                    parts.append(cell + " | ")
                continue
            is_block = child.name in _BLOCK_TAGS
            if is_block:
                parts.append("\n")
            walk(child)
            if is_block:
                parts.append("\n")

    walk(root)
    lines = [collapse_ws(line).rstrip(" |") for line in "".join(parts).split("\n")]
    return "\n".join(line for line in lines if line)


def extract_article_page(html: str | bytes, url: str) -> ArticlePage:
    """Extract headline, date, lead image and body text from an article page."""
    soup = _make_soup(html)
    page = ArticlePage(url=url)

    h1 = soup.find("h1")
    page.h1 = collapse_ws(h1.get_text(" ", strip=True)) if h1 else ""
    page.meta_title = _meta(soup, "og:title", "twitter:title") or (
        collapse_ws(soup.title.get_text()) if soup.title else ""
    )

    date_candidates: list[Any] = [
        _meta(soup, "article:published_time", "datePublished", "pubdate", "publishdate",
              "publish-date", "date", "dc.date", "DC.date.issued", "sailthru.date"),
        *_json_ld_values(soup, "datePublished"),
    ]
    for candidate in date_candidates:
        parsed = parse_date(candidate)
        if parsed:
            page.published = parsed
            break

    # Captured before scripts and page chrome are stripped below.
    social_images = [
        _meta(soup, "og:image", "og:image:url", "og:image:secure_url"),
        _meta(soup, "twitter:image", "twitter:image:src"),
        _link_href(soup, "image_src"),
        *(_image_from_ld(v) for v in _json_ld_values(soup, "image")),
    ]
    raw_text = html.decode("utf-8", "ignore") if isinstance(html, bytes) else html
    script_text = "\n".join(s.string or s.get_text() or "" for s in soup.find_all("script"))
    headline_words = _headline_words(page.h1 or page.meta_title, url)
    _rescue_noscript_images(soup)

    _strip_page_chrome(soup)
    root = _pick_main_root(soup)
    if page.published is None:
        time_tag = root.find("time", datetime=True)
        if time_tag is not None:
            page.published = parse_date(time_tag["datetime"])

    for tag in root.find_all(True):
        if tag.decomposed:
            continue
        marker = " ".join([tag.get("id") or ""] + list(tag.get("class") or [])).lower()
        if any(hint in marker for hint in _ARTICLE_NOISE_HINTS):
            if len(tag.get_text(" ", strip=True)) < 2000:
                tag.decompose()
    for tag in root.find_all(["aside", "nav", "form"]):
        if tag.decomposed:
            continue
        # ASP.NET pages (Sitefinity, e.g. bourns.com) wrap the whole page in one <form>:
        # only small forms (search boxes, sign-ups) are page furniture.
        if tag.name == "form" and len(tag.get_text(" ", strip=True)) >= 1500:
            continue
        tag.decompose()

    # All usable images: the publisher's share images, then images inside the article
    # body (after related-article/share widgets were removed above), then images that
    # only appear in the page's script data and match the headline. Which one is used is
    # decided later (rank_images); generic site-wide pictures are flagged here.
    seen_images: set[str] = set()

    def add_image(src: str, alt: str, origin: str) -> None:
        if not src or src.startswith("data:"):
            return
        if ",http" in src:
            # Some CMSes join base and path into one meta value ("https://a.org,https://a.org/x.png").
            src = "http" + src.rsplit(",http", 1)[1]
        absolute = clean_url(urljoin(url, src.strip()))
        variant = _full_size_variant(absolute)
        for candidate in ([variant] if variant else []) + [absolute]:
            # A share image the publisher set from its theme folder is kept as a site-wide
            # last resort (better than no picture); theme art elsewhere is never used.
            share_from_theme = origin == "social" and _template_path(candidate)
            if candidate in seen_images or not _good_image(candidate, allow_template_path=share_from_theme):
                continue
            seen_images.add(candidate)
            page.images.append(
                ImageRef(
                    url=candidate,
                    alt=collapse_ws(alt),
                    origin=origin,
                    generic=share_from_theme or generic_image_name(candidate),
                )
            )

    for candidate in social_images:
        add_image(candidate, "", "social")
    def own_images() -> int:
        return sum(1 for i in page.images if not i.generic)

    for tag in root.find_all(["img", "a"]):
        # Pages without an <article>/<main> are read from <body>, where header and menu
        # tiles come first: only the article's own images count toward the cap.
        if own_images() >= 16 or len(page.images) >= 40:
            break
        if tag.name == "a":
            # A link straight to an image file: usually the full-size original of the
            # thumbnail it wraps, or a "download high-resolution image" link.
            href = str(tag.get("href") or "")
            if _IMAGE_FILE.search(urlparse(href).path + "?"):
                add_image(href, tag.get_text(" ", strip=True) or str(tag.get("title") or ""), "content")
            continue
        if _displayed_small(tag):
            continue
        add_image(_img_src(tag), str(tag.get("alt") or tag.get("title") or ""), "content")
    for src, alt in _embedded_images(raw_text, script_text, url, headline_words):
        add_image(src, alt, "embedded")

    page.image_url = next(
        (i.url for i in page.images if not i.generic),
        page.images[0].url if page.images else None,
    )
    page.text = _render_text(root)[:_MAX_ARTICLE_CHARS]
    return page


def _link_href(soup: BeautifulSoup, rel: str) -> str:
    tag = soup.find("link", rel=rel, href=True)
    return str(tag["href"]) if tag is not None else ""


def _rescue_noscript_images(soup: BeautifulSoup) -> None:
    """Lazy-loading pages often put the real <img> only inside <noscript>, which is
    dropped with the page chrome: move those images out first."""
    for block in soup.find_all("noscript"):
        images = block.find_all("img") or _make_soup(block.get_text()).find_all("img")
        for img in images:
            block.insert_before(img.extract() if img.parent is not None else img)


# ---------------------------------------------------------------------------
# RSS / Atom feeds
# ---------------------------------------------------------------------------


_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class FeedItem:
    title: str
    url: str
    published: date | None


def parse_feed(content: bytes, base_url: str) -> list[FeedItem]:
    """Parse an RSS/Atom document into feed items (empty list if not a valid feed)."""
    parsed = feedparser.parse(content)
    items: list[FeedItem] = []
    for entry in parsed.entries:
        title = collapse_ws(html_lib.unescape(_TAG_RE.sub(" ", entry.get("title", ""))))
        link = entry.get("link") or ""
        if not title or not link:
            continue
        absolute = clean_url(urljoin(base_url, link))
        if not _is_http_link(absolute):
            continue
        published: date | None = None
        for attr in ("published_parsed", "updated_parsed"):
            struct = entry.get(attr)
            # Some feeds emit placeholder dates (e.g. 1970-01-01); treat those as unknown.
            if struct and struct.tm_year >= 2000:
                published = date(struct.tm_year, struct.tm_mon, struct.tm_mday)
                break
        items.append(FeedItem(title=title, url=absolute, published=published))
    return items
