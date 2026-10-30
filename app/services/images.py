"""
Images for EE Herald articles.

Images are taken live from the source news page. Candidates are ranked so the article's
own pictures come first (rank_images): the publisher's share image or the image picked
with the news item, then images inside the article body, then images found only in the
page's script data. Site-wide pictures - a default share card, a brand banner, an image
the site attached to other articles too - drop to the end and are only used when the
article has nothing of its own. Logos, icons, buttons, banners and duplicates are
skipped. Downloads are free direct fetches.

The original file is kept unmodified; the article uses an EE Herald-format copy
(1200 x 800 px PNG, the size EE Herald publishes).
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import date
from io import BytesIO
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from PIL import Image, ImageOps

from app.infra.http import HttpFetcher
from app.services.parsing import ImageRef, headline_words, upload_month

if TYPE_CHECKING:
    from app.services.image_history import ImageHistory

logger = logging.getLogger(__name__)

_MIN_WIDTH = 400
_MIN_HEIGHT = 250
# A smaller picture is only used when the article has no image of the normal size.
_FALLBACK_MIN_WIDTH = 300
_FALLBACK_MIN_HEIGHT = 200
_MAX_ASPECT = 3.5  # wider than this is a banner/strip
_MIN_ASPECT = 0.4  # taller than this is a sidebar/skyscraper
_DUPLICATE_DISTANCE = 6  # max differing bits of the 64-bit average hash
# Formats kept byte-for-byte. Anything else is skipped rather than converted (no edits).
_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "AVIF": "avif"}
# Image requests ask for formats every step here (and every browser) reads, so CDNs
# that choose the format by Accept send JPEG/PNG/WebP.
IMAGE_ACCEPT = "image/webp,image/png,image/jpeg;q=0.9,image/*;q=0.5,*/*;q=0.3"
# The image picked with the news item leads its kind (as before), then share images.
_ORIGIN_SCORE = {"news": 3.2, "social": 3, "content": 2, "embedded": 1}
# A picture the site attached to other articles is often still a relevant product photo;
# one named as a default/brand/share card is plain site art and ranks below it.
_REUSED_PENALTY = 10
_NAMED_GENERIC_PENALTY = 12
# Orders images within the same kind only: a publisher's own (non-generic) share image
# stays ahead of body images, which is right for nearly every newsroom.
_HEADLINE_MATCH_BONUS = 0.5
# A picture uploaded this many months (or more) before the article was published is
# probably a stock photo or an older release's picture (e.g. a 2026/04 upload on an
# October announcement). It drops below the article's other pictures but is still used,
# with a note, when nothing else exists. One month apart is normal: press photos are
# often uploaded a few weeks before the release goes out.
_OLD_UPLOAD_MONTHS = 2
_OLD_UPLOAD_PENALTY = 1.5


def is_old_upload(url: str, published: date | None) -> bool:
    """True when the image URL's upload folder is _OLD_UPLOAD_MONTHS+ before `published`."""
    month = upload_month(url)
    if month is None or published is None:
        return False
    return (published.year * 12 + published.month) - (month[0] * 12 + month[1]) >= _OLD_UPLOAD_MONTHS


def rank_images(
    refs: list[ImageRef],
    headline: str,
    article_url: str,
    history: "ImageHistory | None" = None,
    published: date | None = None,
) -> list[ImageRef]:
    """Order image candidates: the article's own pictures first, site-wide ones last.

    A picture is generic when its file name says so (default/share/brand...) or the site
    already attached it to a different article. A file name or alt text sharing a
    distinctive headline word moves an image up; an upload months older than the article
    (`published`) moves it down. Ties keep the page order.
    """
    words = headline_words(headline, article_url)
    scored = []
    for index, ref in enumerate(refs):
        reused = history is not None and history.is_generic(article_url, ref.url)
        generic = ref.generic or reused
        old = is_old_upload(ref.url, published)
        name = f"{urlparse(ref.url).path.rsplit('/', 1)[-1]} {ref.alt}".lower()
        score = _ORIGIN_SCORE.get(ref.origin, 1)
        if any(word in name for word in words):
            score += _HEADLINE_MATCH_BONUS
        if ref.generic:
            score -= _NAMED_GENERIC_PENALTY
        elif reused:
            score -= _REUSED_PENALTY
        if old:
            score -= _OLD_UPLOAD_PENALTY
        scored.append((-score, index, replace(ref, generic=generic, old=old)))
    return [ref for _, _, ref in sorted(scored, key=lambda item: (item[0], item[1]))]


@dataclass
class DownloadedImage:
    source_url: str
    alt: str
    origin: str
    data: bytes  # the original file, unmodified
    extension: str  # "jpg" | "png" | "webp" | "avif"
    width: int
    height: int
    fingerprint: int
    generic: bool = False  # a site-wide picture, used only for lack of anything else
    small: bool = False  # below the normal minimum size (a last resort)
    old: bool = False  # uploaded months before the article (see is_old_upload)


def _average_hash(img: Image.Image) -> int:
    small = img.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
    pixels = list(small.tobytes())  # one byte per pixel in "L" mode
    mean = sum(pixels) / len(pixels)
    bits = 0
    for value in pixels:
        bits = (bits << 1) | (1 if value >= mean else 0)
    return bits


def inspect_image(
    data: bytes, min_width: int = _MIN_WIDTH, min_height: int = _MIN_HEIGHT
) -> tuple[str, int, int, int] | None:
    """Check that `data` is a usable article image (the bytes are never modified).

    Returns (extension, width, height, fingerprint), or None for icons, buttons,
    banners, unreadable files and formats that would need converting.
    """
    try:
        with Image.open(BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            if fmt not in _FORMATS:
                return None
            img.load()
            width, height = img.size
            if width < min_width or height < min_height:
                return None
            aspect = width / height
            if aspect > _MAX_ASPECT or aspect < _MIN_ASPECT:
                return None
            return _FORMATS[fmt], width, height, _average_hash(img)
    except Exception:
        return None


async def download_images(
    refs: list[ImageRef], referer: str, http: HttpFetcher, limit: int = 6
) -> tuple[list[DownloadedImage], list[str]]:
    """Download candidate images live from the source site, in the given (ranked) order.

    Returns (usable images in priority order, URLs that could not be downloaded or were
    not usable). Near-duplicates (the same picture at another size) keep the
    higher-priority copy. Pictures below the normal size are only returned when nothing
    of the normal size was found.
    """

    async def fetch(ref: ImageRef) -> DownloadedImage | None:
        try:
            result = await http.fetch(ref.url, referer=referer, accept=IMAGE_ACCEPT)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Image download failed for %s: %s", ref.url, exc)
            return None
        if result is None or not result.ok or not result.content:
            return None
        inspected = await asyncio.to_thread(
            inspect_image, result.content, _FALLBACK_MIN_WIDTH, _FALLBACK_MIN_HEIGHT
        )
        if inspected is None:
            return None
        ext, width, height, fingerprint = inspected
        return DownloadedImage(
            ref.url, ref.alt, ref.origin, result.content, ext, width, height, fingerprint,
            generic=ref.generic,
            small=width < _MIN_WIDTH or height < _MIN_HEIGHT,
            old=ref.old,
        )

    candidates = refs[:16]
    downloaded = await asyncio.gather(*(fetch(r) for r in candidates))

    usable: list[DownloadedImage] = []
    failed: list[str] = []
    for ref, image in zip(candidates, downloaded):
        if image is None:
            failed.append(ref.url)
            continue
        if any(
            bin(k.fingerprint ^ image.fingerprint).count("1") <= _DUPLICATE_DISTANCE for k in usable
        ):
            continue
        usable.append(image)

    # The article's own full-size pictures, then its own smaller ones, then site-wide
    # pictures. Smaller pictures are dropped when a full-size own picture exists.
    own_full = [i for i in usable if not i.generic and not i.small]
    own_small = [] if own_full else [i for i in usable if not i.generic and i.small]
    site_wide = [i for i in usable if i.generic and (not i.small or not own_full)]
    kept = own_full + own_small + site_wide
    return kept[:limit], failed


FEATURED_SIZE = (1200, 800)
# Images whose shape is within this tolerance of 3:2 are centre-cropped; others are fitted
# onto a white background so product shots and diagrams are never cut off.
_CROP_TOLERANCE = 0.2


def _flatten(img: Image.Image) -> Image.Image:
    """RGB image; transparent areas become white."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert("RGB")


def make_featured(data: bytes, size: tuple[int, int] = FEATURED_SIZE) -> bytes:
    """Produce the EE Herald article image: exactly `size` (1200 x 800) as PNG."""
    target_w, target_h = size
    with Image.open(BytesIO(data)) as img:
        img = _flatten(ImageOps.exif_transpose(img))
        if abs((img.width / img.height) / (target_w / target_h) - 1) <= _CROP_TOLERANCE:
            result = ImageOps.fit(img, size, Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        else:
            fitted = ImageOps.contain(img, size, Image.Resampling.LANCZOS)
            result = Image.new("RGB", size, (255, 255, 255))
            result.paste(fitted, ((target_w - fitted.width) // 2, (target_h - fitted.height) // 2))
        out = BytesIO()
        result.save(out, format="PNG", optimize=True)
        return out.getvalue()


def describe_alt(alt: str, headline: str) -> str:
    """Alt text: the source's own description if meaningful, otherwise the headline."""
    alt = alt.strip()
    looks_like_filename = " " not in alt and ("-" in alt or "_" in alt or "." in alt)
    if len(alt.split()) >= 3 and not looks_like_filename:
        return alt
    return headline
