"""Image selection: the article's own picture first, site-wide pictures last."""

import asyncio
from io import BytesIO
from pathlib import Path

from PIL import Image

from app.infra.http import FetchResult
from app.services.image_history import ImageHistory
from app.services.images import download_images, inspect_image, rank_images
from app.services.parsing import (
    ImageRef,
    _full_size_variant,
    extract_article_page,
    generic_image_name,
)


def _png(width: int, height: int, color: tuple[int, int, int] = (40, 90, 160), pattern: bool = False) -> bytes:
    img = Image.new("RGB", (width, height), color)
    if pattern:
        for x in range(0, width, max(1, width // 8)):
            for y in range(height):
                img.putpixel((x, y), (250, 250, 250))
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def test_generic_image_names() -> None:
    generic = [
        "https://www.sony-semicon.com/files/62/ogp-top.jpeg",
        "https://counterpointresearch.com/images/og/og-default.png",
        "https://www.semi.org/sites/semi.org/files/2021-10/semi-brand-social.png",
        "https://brainchip.com/wp-content/uploads/2024/05/1.-BC-general-thumbnail-1.png",
        "https://x.com/og-image.png",
    ]
    own = [
        # Apple's own share image: "og" alone does not make a picture generic
        "https://www.apple.com/newsroom/images/2026/10/x/tile/Apple-TV-4K-261013-lp.jpg.og.jpg",
        "https://www.purdue.edu/newsroom/wp-content/uploads/2026/10/GlobalFoundries-OG.jpg",
        "https://x.com/shareholder-meeting-2026.jpg",  # whole words only
        "https://x.com/smart-card-reader.jpg",
    ]
    assert all(generic_image_name(u) for u in generic)
    assert not any(generic_image_name(u) for u in own)


def test_full_size_variant() -> None:
    assert _full_size_variant("https://x.com/wp/photo-1024x576.jpg?v=2") == "https://x.com/wp/photo.jpg"
    assert _full_size_variant("https://x.com/p/PR123_200px.png") == "https://x.com/p/PR123.png"
    assert _full_size_variant("https://x.com/news/pr.tmb-small.jpg") == "https://x.com/news/pr.jpg"
    assert _full_size_variant("https://x.com/news/photo.jpg") is None


PAGE = """<html><head>
<meta property="og:image" content="https://x.com/img/og-default.png">
<script>window.__DATA__ = {"body": "\\u003cp\\u003eText\\u003c/p\\u003e\\u003cimg src=\\"https://cdn.x.com/1280_abcd.jpg\\" alt=\\"X100 module\\"\\u003e"};
self.push(["https://cdn.x.com/media/News/2026/Sentry50.png", "https://cdn.x.com/media/Common/Logo.png"]);</script>
</head><body>
<header><img src="https://x.com/img/topnav-products.jpg" width="200"></header>
<article><h1>X Launches Sentry 5.0 Security Module</h1>
<p>SAN JOSE - X today launched Sentry 5.0, a security module for servers. It adds post-quantum
cryptography and works with existing boards. Samples are available now, with volume production
planned for early next year. The module ships with reference designs and software.</p>
<img src="data:image/gif;base64,R0lGOD" data-lazy-srcset="https://x.com/img/hero-600.jpg 600w, https://x.com/img/hero-1600.jpg 1600w" width="280" alt="Sentry board">
<noscript><img src="https://x.com/img/board-closeup.jpg" alt="Board close-up"></noscript>
<a href="https://x.com/img/sentry-hires.png"><img src="https://x.com/img/sentry-hires.tmb-small.png" width="120"></a>
</article></body></html>"""


def test_extract_finds_the_articles_own_images() -> None:
    page = extract_article_page(PAGE, "https://x.com/news/sentry-5")
    urls = [i.url for i in page.images]
    by_url = {i.url: i for i in page.images}
    assert by_url["https://x.com/img/og-default.png"].generic  # site default share card
    assert "https://x.com/img/hero-1600.jpg" in urls  # lazy srcset, large entry, despite width=280
    assert "https://x.com/img/board-closeup.jpg" in urls  # rescued from <noscript>
    assert "https://x.com/img/sentry-hires.png" in urls  # link to the full-size original
    assert "https://cdn.x.com/1280_abcd.jpg" in urls  # <img> inside the CMS's JSON body
    assert by_url["https://cdn.x.com/1280_abcd.jpg"].alt == "X100 module"
    assert "https://cdn.x.com/media/News/2026/Sentry50.png" in urls  # script URL matching the headline
    assert "https://cdn.x.com/media/Common/Logo.png" not in urls
    assert "https://x.com/img/topnav-products.jpg" not in urls  # page header removed
    # The page's lead image is never the site default when an own image exists.
    assert page.image_url != "https://x.com/img/og-default.png"


def test_header_tiles_do_not_crowd_out_the_article_image() -> None:
    tiles = "".join(f'<img src="https://x.com/images/hfc/topnav-item{i}.jpg">' for i in range(30))
    html = f"""<html><body><div class="menu">{tiles}</div><div class="content">
    <p>{"Body text of the release. " * 40}</p>
    <a href="/images/news/pr.jpg"><img src="/images/news/pr.tmb-small.jpg" alt="PR Image"></a>
    </div></body></html>"""
    page = extract_article_page(html, "https://x.com/news/pr")
    assert "https://x.com/images/news/pr.jpg" in [i.url for i in page.images]


def test_rank_images_puts_site_wide_pictures_last(tmp_path: Path) -> None:
    history = ImageHistory(tmp_path / "image_history.json")
    history.record("https://x.com/news/older-release", ["https://x.com/img/press-photo.jpg"])
    refs = [
        ImageRef("https://x.com/img/press-photo.jpg", origin="social"),  # used on another article
        ImageRef("https://x.com/img/og-default.png", origin="news", generic=True),
        ImageRef("https://x.com/img/diagram.png", "Block diagram", origin="content"),
        ImageRef("https://x.com/img/sentry-module.png", "", origin="content"),
    ]
    ranked = rank_images(refs, "X launches Sentry 5.0 module", "https://x.com/news/sentry", history)
    # Own pictures first (the one matching the headline leads its kind), site-wide last.
    assert [r.url.rsplit("/", 1)[-1] for r in ranked] == [
        "sentry-module.png", "diagram.png", "press-photo.jpg", "og-default.png",
    ]
    assert ranked[2].generic and ranked[3].generic


def test_history_ignores_the_same_article_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "image_history.json"
    history = ImageHistory(path)
    history.record("https://www.x.com/news/a?utm_source=x", ["https://x.com/img/a.jpg?w=800"])
    assert not history.is_generic("https://x.com/news/a", "https://x.com/img/a.jpg")  # same article
    assert history.is_generic("https://x.com/news/b", "https://x.com/img/a.jpg?w=1600")
    history.save()
    assert path.exists() and "x.com" in path.read_text(encoding="utf-8")


def test_small_own_image_beats_a_site_wide_one() -> None:
    files = {
        "https://x.com/brand.png": _png(1200, 630, (200, 30, 30)),
        "https://x.com/own-small.png": _png(370, 260, (30, 160, 60), pattern=True),
        "https://x.com/icon.png": _png(120, 120),
    }

    class FakeHttp:
        async def fetch(self, url: str, referer: str | None = None, accept: str | None = None) -> FetchResult:
            return FetchResult(url, url, 200, files[url], "image/png")

    refs = [
        ImageRef("https://x.com/own-small.png", origin="embedded"),
        ImageRef("https://x.com/icon.png", origin="content"),
        ImageRef("https://x.com/brand.png", origin="social", generic=True),
    ]
    kept, failed = asyncio.run(download_images(refs, "https://x.com/news", FakeHttp()))  # type: ignore[arg-type]
    assert [k.source_url for k in kept] == ["https://x.com/own-small.png", "https://x.com/brand.png"]
    assert kept[0].small and not kept[0].generic and kept[1].generic
    assert failed == ["https://x.com/icon.png"]


def test_avif_images_are_accepted() -> None:
    out = BytesIO()
    Image.new("RGB", (800, 500), (10, 120, 200)).save(out, format="AVIF")
    info = inspect_image(out.getvalue())
    assert info is not None and info[:3] == ("avif", 800, 500)
