"""
Which images a site attaches to many different articles.

Many newsrooms set one site-wide share image (og:image) - a brand banner, a logo card,
a stock photo of the headquarters - and send it with every page. Such a picture is not
the article's own image, so it should only ever be a last resort.

The file maps a site to the images seen on its articles and which articles they were
seen on. An image already seen on a *different* article of the same site counts as
generic for this one. The history is small (bounded per site), lives next to the state
file, and on first use is seeded from the digests and drafts already on disk, so known
site-wide images are recognised from the first run.
"""

import json
import logging
import threading
from pathlib import Path
from urllib.parse import urlsplit

from app.services.parsing import url_key

logger = logging.getLogger(__name__)

FILE_NAME = "image_history.json"
_MAX_IMAGES_PER_SITE = 400
_MAX_ARTICLES_PER_IMAGE = 6

_lock = threading.Lock()
_cache: dict[str, "ImageHistory"] = {}


def _site(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def image_key(url: str) -> str:
    """Same picture at another size or with tracking parameters -> same key."""
    parts = urlsplit(url)
    return f"{(parts.hostname or '').lower()}{parts.path}"


class ImageHistory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, list[str]]] = {}
        self._dirty = False

    def is_generic(self, article_url: str, image_url: str) -> bool:
        """True if this image was already attached to a different article of the site."""
        seen = self._data.get(_site(article_url), {}).get(image_key(image_url), [])
        this = url_key(article_url)
        return any(other != this for other in seen)

    def record(self, article_url: str, image_urls: list[str]) -> None:
        site, this = _site(article_url), url_key(article_url)
        if not site:
            return
        with _lock:
            images = self._data.setdefault(site, {})
            for url in image_urls:
                key = image_key(url)
                articles = images.setdefault(key, [])
                if this not in articles:
                    articles.append(this)
                    del articles[:-_MAX_ARTICLES_PER_IMAGE]
                    self._dirty = True
            # Bounded: forget the oldest images of a site first (dicts keep insertion order).
            while len(images) > _MAX_IMAGES_PER_SITE:
                images.pop(next(iter(images)))

    def save(self) -> None:
        with _lock:
            if not self._dirty:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data, separators=(",", ":")), encoding="utf-8")
                tmp.replace(self.path)
                self._dirty = False
            except OSError as exc:
                logger.warning("Could not save the image history: %s", exc)


def _seed(history: ImageHistory, output_dir: Path) -> None:
    """Build the history from the digests and drafts already on disk."""
    for path in sorted(output_dir.glob("News_Digest_*.json")):
        try:
            articles = json.loads(path.read_text(encoding="utf-8")).get("articles", [])
        except (OSError, ValueError, AttributeError):
            continue
        for article in articles:
            if isinstance(article, dict) and article.get("url") and article.get("image_url"):
                history.record(article["url"], [article["image_url"]])
    for path in sorted((output_dir / "press_releases").glob("*.json")):
        try:
            draft = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        images = [i.get("source_url") for i in draft.get("images") or [] if i.get("source_url")]
        if draft.get("source_url") and images:
            # Only the image that was used: the rest of a page's images were never
            # judged, and a gallery image should not count as "seen" yet.
            history.record(draft["source_url"], images[:1])


def get_history(state_file: str, output_dir: str) -> ImageHistory:
    """The shared history for this install (loaded once per process)."""
    path = Path(state_file).with_name(FILE_NAME)
    with _lock:
        cached = _cache.get(str(path))
    if cached is not None:
        return cached
    history = ImageHistory(path)
    try:
        history._data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _seed(history, Path(output_dir))
        history.save()
        logger.info("Image history seeded for %d sites.", len(history._data))
    except (OSError, ValueError) as exc:
        logger.warning("Image history unreadable (%s); starting a new one.", exc)
    with _lock:
        return _cache.setdefault(str(path), history)
