"""
Tests for deterministic parsing helpers (no network).
"""

from datetime import date

from app.services.parsing import (
    build_link_digest,
    clean_url,
    extract_article_page,
    parse_date,
    parse_feed,
    url_key,
)

LISTING_HTML = """
<html><head><title>Newsroom | Example</title></head>
<body>
  <header><nav><a href="/products">Products</a><a href="/about">About us</a></nav></header>
  <div id="onetrust-banner"><a href="/cookies">Cookie settings</a></div>
  <main>
    <article>
      <header><h3><a href="/news/2026/chip-launch?utm_source=x">Example Launches New Chip &amp; SDK</a></h3></header>
      <time datetime="2026-10-01">Oct 1, 2026</time>
      <a href="/news/2026/chip-launch">Read more</a>
    </article>
    <div class="card">
      <a href="https://example.com/news/2026/ceo"><img src="ceo.jpg" alt="CEO photo"></a>
      <h3>Example Appoints New CEO</h3>
      <a href="https://example.com/news/2026/ceo">Learn more</a>
    </div>
    <a href="javascript:void(0)">Load more</a>
    <a href="#top">Back to top</a>
  </main>
  <footer><a href="/privacy">Privacy policy</a></footer>
</body></html>
"""


def test_clean_url_strips_tracking_and_fragment() -> None:
    assert clean_url("https://a.com/x?utm_source=1&id=5#frag") == "https://a.com/x?id=5"
    assert clean_url("https://a.com:443/x") == "https://a.com/x"
    # Untouched when nothing is removed.
    assert clean_url("https://a.com/n?defaultGroupId=false") == "https://a.com/n?defaultGroupId=false"


def test_url_key_equivalence() -> None:
    assert url_key("http://www.Example.com/news/a/") == url_key("https://example.com/news/a")
    assert url_key("https://www.nxp.com:443/x?utm_medium=y") == url_key("https://nxp.com/x")
    assert url_key("https://a.com/x?id=1") != url_key("https://a.com/x?id=2")


def test_link_digest_ids_and_chrome_removal() -> None:
    digest = build_link_digest(LISTING_HTML, "https://example.com/newsroom")
    urls = {link.url for link in digest.links.values()}
    assert "https://example.com/news/2026/chip-launch" in urls
    assert "https://example.com/news/2026/ceo" in urls
    # nav, footer, cookie banner, javascript: and same-page links are gone
    assert not any("/products" in u or "/privacy" in u or "/cookies" in u for u in urls)
    assert len(urls) == 2
    # One ID per unique URL, even when linked several times (title + "Read more").
    assert digest.text.count("#") >= 3
    chip_id = next(i for i, link in digest.links.items() if link.url.endswith("chip-launch"))
    assert digest.text.count(f"(#{chip_id})") == 2
    # Headline inside an <article><header> must be kept; time datetime is surfaced.
    assert "Example Launches New Chip & SDK" in digest.text
    assert "[2026-10-01]" in digest.text
    assert digest.title == "Newsroom | Example"


def test_parse_date_formats() -> None:
    assert parse_date("2026-10-01") == date(2026, 10, 1)
    assert parse_date("2026-10-01T09:00:00Z") == date(2026, 10, 1)
    assert parse_date("October 1, 2026") == date(2026, 10, 1)
    assert parse_date("1 Oct 2026") == date(2026, 10, 1)
    assert parse_date("not a date") is None
    assert parse_date(None) is None


ARTICLE_HTML = """
<html><head>
<title>Chip Launch | Example</title>
<meta property="og:title" content="Example Launches New Chip">
<meta property="og:image" content="/img/hero.jpg">
<script type="application/ld+json">{"@type": "NewsArticle", "datePublished": "2026-10-01T08:00:00Z"}</script>
</head><body>
<nav><a href="/">Home</a></nav>
<article>
  <h1>Example Launches New Chip</h1>
  <p>SAN JOSE, Calif., Oct. 1, 2026 - Example today announced a new chip that does many
  things very well for many customers across many markets and segments worldwide.</p>
  <div class="share-buttons"><a href="#">Share on X</a></div>
  <p>The chip is built on an advanced process and offers industry leading performance per
  watt for edge AI workloads, according to the company, which expects volume production soon.</p>
</article>
</body></html>
"""


def test_extract_article_page() -> None:
    page = extract_article_page(ARTICLE_HTML, "https://example.com/news/chip")
    assert page.h1 == "Example Launches New Chip"
    assert page.published == date(2026, 10, 1)
    assert page.image_url == "https://example.com/img/hero.jpg"
    assert "SAN JOSE" in page.text
    assert "Share on X" not in page.text


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>News</title>
<item><title>First &amp; Best Release</title><link>https://example.com/a?utm_source=rss</link>
<pubDate>Thu, 01 Oct 2026 05:00:00 GMT</pubDate></item>
<item><title></title><link>https://example.com/b</link></item>
</channel></rss>"""


def test_parse_feed() -> None:
    items = parse_feed(RSS, "https://example.com/feed")
    assert len(items) == 1
    assert items[0].title == "First & Best Release"
    assert items[0].url == "https://example.com/a"
    assert items[0].published == date(2026, 10, 1)


def test_social_links_are_not_candidates() -> None:
    html = """<body><main>
    <a href="https://www.linkedin.com/posts/abc">Our CEO on LinkedIn about AI chips</a>
    <a href="https://example.com/news/real">A real press release headline</a>
    </main></body>"""
    digest = build_link_digest(html, "https://example.com/news")
    assert [link.url for link in digest.links.values()] == ["https://example.com/news/real"]
    assert "Our CEO on LinkedIn" in digest.text  # still visible as context


def test_feed_placeholder_dates_ignored() -> None:
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Old style</title><link>https://example.com/x</link>
    <pubDate>Thu, 01 Jan 1970 00:00:31 GMT</pubDate></item></channel></rss>"""
    assert parse_feed(rss, "https://example.com")[0].published is None
