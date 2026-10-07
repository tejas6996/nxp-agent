"""
Images for EE Herald articles.

Images are taken live from the source news page. Priority: the image extracted with the
news item (shown in the digest) first, then the publisher's share image, then images
inside the article body. Logos, icons, buttons, banners and duplicates are skipped.
Downloads are free direct fetches.

The original file is kept unmodified; the article uses an EE Herald-format copy
(1200 x 800 px PNG, the size EE Herald publishes).
"""

import asyncio
import logging
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

from app.infra.http import HttpFetcher
from app.services.parsing import ImageRef

logger = logging.getLogger(__name__)

_MIN_WIDTH = 400
_MIN_HEIGHT = 250
_MAX_ASPECT = 3.5  # wider than this is a banner/strip
_MIN_ASPECT = 0.4  # taller than this is a sidebar/skyscraper
_DUPLICATE_DISTANCE = 6  # max differing bits of the 64-bit average hash
# Formats kept byte-for-byte. Anything else is skipped rather than converted (no edits).
_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
_ORIGIN_PRIORITY = {"news": 0, "social": 1, "content": 2}


@dataclass
class DownloadedImage:
    source_url: str
    alt: str
    origin: str
    data: bytes  # the original file, unmodified
    extension: str  # "jpg" | "png" | "webp"
    width: int
    height: int
    fingerprint: int


def _average_hash(img: Image.Image) -> int:
    small = img.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
    pixels = list(small.tobytes())  # one byte per pixel in "L" mode
    mean = sum(pixels) / len(pixels)
    bits = 0
    for value in pixels:
        bits = (bits << 1) | (1 if value >= mean else 0)
    return bits


def inspect_image(data: bytes) -> tuple[str, int, int, int] | None:
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
            if width < _MIN_WIDTH or height < _MIN_HEIGHT:
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
    """Download candidate images live from the source site.

    Returns (usable images in priority order, URLs that could not be downloaded or were
    not usable). Near-duplicates (the same picture at another size) keep the
    higher-priority copy - e.g. the image extracted with the news item.
    """
    ordered = sorted(refs, key=lambda r: _ORIGIN_PRIORITY.get(r.origin, 3))

    async def fetch(ref: ImageRef) -> DownloadedImage | None:
        try:
            result = await http.fetch(ref.url, referer=referer)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Image download failed for %s: %s", ref.url, exc)
            return None
        if result is None or not result.ok or not result.content:
            return None
        inspected = await asyncio.to_thread(inspect_image, result.content)
        if inspected is None:
            return None
        ext, width, height, fingerprint = inspected
        return DownloadedImage(
            ref.url, ref.alt, ref.origin, result.content, ext, width, height, fingerprint
        )

    candidates = ordered[:14]
    downloaded = await asyncio.gather(*(fetch(r) for r in candidates))

    kept: list[DownloadedImage] = []
    failed: list[str] = []
    for ref, image in zip(candidates, downloaded):
        if image is None:
            failed.append(ref.url)
            continue
        if any(
            bin(k.fingerprint ^ image.fingerprint).count("1") <= _DUPLICATE_DISTANCE for k in kept
        ):
            continue
        kept.append(image)
        if len(kept) >= limit:
            break
    return kept, failed


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
