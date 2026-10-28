"""
Editor-controlled settings per source, kept next to the state file.

Sources are added in code (app/sites.py); from the EE Herald admin an editor can
only *ignore* a source (stop checking it) or resume it. Ignoring is reversible and
removes nothing: the site keeps its state, so resuming it does not re-report old
articles.

File: <state file folder>/site_settings.json
    {"ignored": {"<site url>": {"at": "2026-10-20T21:00:00", "by": "admin 12"}}}
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.settings import Settings

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


def settings_path(settings: Settings) -> Path:
    return Path(settings.state_file).with_name("site_settings.json")


def load_site_settings(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ignored": {}}
    except (OSError, json.JSONDecodeError) as exc:
        # A damaged file must never stop the digest: check every source.
        logger.error("Could not read %s (%s); no sources are ignored.", path, exc)
        return {"ignored": {}}
    if not isinstance(data, dict) or not isinstance(data.get("ignored"), dict):
        return {"ignored": {}}
    return data


def ignored_urls(settings: Settings) -> set[str]:
    return set(load_site_settings(settings_path(settings))["ignored"])


def active_sites(sites: list[dict[str, str]], settings: Settings) -> list[dict[str, str]]:
    """The configured sites minus the ones an editor has ignored."""
    ignored = ignored_urls(settings)
    if ignored:
        logger.info("%d source(s) ignored by an editor are not checked.", len(ignored))
    return [s for s in sites if s["url"] not in ignored]


def set_ignored(settings: Settings, url: str, ignored: bool, by: str = "") -> dict[str, Any]:
    """Ignore or resume one source (atomic write). Returns the stored entry or {}."""
    path = settings_path(settings)
    with _LOCK:
        data = load_site_settings(path)
        if ignored:
            data["ignored"][url] = {
                "at": datetime.now().replace(microsecond=0).isoformat(),
                "by": str(by)[:60],
            }
        else:
            data["ignored"].pop(url, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".site_settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return data["ignored"].get(url, {})


# ---------------------------------------------------------------------------
# Newsrooms added from the EE Herald admin ("Add newsroom")
# ---------------------------------------------------------------------------
# Stored in <state file folder>/custom_sites.json and merged with app/sites.py on
# every run. Entries are only created from a successful check (see the
# /sources/check endpoints), validated again whenever the file is read, and can be
# removed again by an editor. Built-in sources can only be ignored, not removed.

import re as _re  # noqa: E402

from app.infra.url_guard import UnsafeURLError, basic_check  # noqa: E402

MAX_CUSTOM_SITES = 500
_NAME_RE = _re.compile(r"^[\w .,&'()+/®™\-–]{2,80}$", _re.UNICODE)


def custom_path(settings: Settings) -> Path:
    return Path(settings.state_file).with_name("custom_sites.json")


def clean_name(name: str) -> str:
    """Tidy and check a source name (it is printed in the PDF digest)."""
    text = _re.sub(r"\s+", " ", str(name or "")).strip()
    if not _NAME_RE.match(text):
        raise ValueError("Use 2-80 letters, numbers, spaces or . , & ' ( ) + / - for the name.")
    return text


def _valid_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        name = clean_name(raw.get("name", ""))
        url = str(raw.get("url", ""))
        basic_check(url)
        out: dict[str, Any] = {"name": name, "url": url}
        if raw.get("feed"):
            basic_check(str(raw["feed"]))
            out["feed"] = str(raw["feed"])
        hours = raw.get("firecrawl_cooldown_hours")
        if hours is not None:
            out["firecrawl_cooldown_hours"] = max(1, min(168, int(hours)))
        out["custom"] = True
        out["added_at"] = str(raw.get("added_at", ""))[:32]
        out["added_by"] = str(raw.get("added_by", ""))[:60]
        return out
    except (ValueError, UnsafeURLError, TypeError):
        return None


def load_custom_sites(settings: Settings) -> list[dict[str, Any]]:
    try:
        data = json.loads(custom_path(settings).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Could not read %s (%s); editor-added sources are skipped.", custom_path(settings), exc)
        return []
    rows = data.get("sites") if isinstance(data, dict) else None
    return [e for e in (_valid_entry(r) for r in (rows if isinstance(rows, list) else [])) if e]


def all_sites(settings: Settings) -> list[dict[str, Any]]:
    """Built-in sources (app/sites.py) plus the ones editors added."""
    from app.services.parsing import url_key
    from app.sites import SITES

    keys = {url_key(s["url"]) for s in SITES}
    names = {s["name"].casefold() for s in SITES}
    out = list(SITES)
    for entry in load_custom_sites(settings):
        if url_key(entry["url"]) in keys or entry["name"].casefold() in names:
            continue
        keys.add(url_key(entry["url"]))
        names.add(entry["name"].casefold())
        out.append(entry)
    return out


def find_tracked(urls: list[str], settings: Settings) -> dict[str, Any] | None:
    """The source already reading one of these URLs (listing page or feed), if any."""
    from app.services.parsing import url_key

    wanted = {url_key(u) for u in urls if u}
    for site in all_sites(settings):
        if url_key(site["url"]) in wanted or (site.get("feed") and url_key(site["feed"]) in wanted):
            return site
    return None


def _write_custom(settings: Settings, rows: list[dict[str, Any]]) -> None:
    path = custom_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".custom_sites.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"sites": rows}, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def add_custom_site(settings: Settings, entry: dict[str, Any], by: str = "") -> dict[str, Any]:
    """Store a checked newsroom. Raises ValueError with a message for the editor."""
    name = clean_name(entry.get("name", ""))
    with _LOCK:
        rows = load_custom_sites(settings)
        if len(rows) >= MAX_CUSTOM_SITES:
            raise ValueError(f"At most {MAX_CUSTOM_SITES} newsrooms can be added from the admin.")
        if any(s["name"].casefold() == name.casefold() for s in all_sites(settings)):
            raise ValueError(f"A source called {name} already exists; choose another name.")
        dup = find_tracked([entry["url"], entry.get("feed") or ""], settings)
        if dup:
            raise ValueError(f"This newsroom is already tracked as {dup['name']}.")
        new = {
            "name": name,
            "url": entry["url"],
            **({"feed": entry["feed"]} if entry.get("feed") else {}),
            **({"firecrawl_cooldown_hours": 24} if entry.get("firecrawl") else {}),
            "added_at": datetime.now().replace(microsecond=0).isoformat(),
            "added_by": str(by)[:60],
        }
        if _valid_entry(new) is None:
            raise ValueError("This newsroom could not be saved (invalid link).")
        store = [{k: v for k, v in r.items() if k != "custom"} for r in rows] + [new]
        _write_custom(settings, store)
    return {**new, "custom": True}


def remove_custom_site(settings: Settings, url: str) -> bool:
    """Delete an editor-added newsroom (built-in ones cannot be removed)."""
    with _LOCK:
        rows = load_custom_sites(settings)
        keep = [r for r in rows if r["url"] != url]
        if len(keep) == len(rows):
            return False
        _write_custom(settings, [{k: v for k, v in r.items() if k != "custom"} for r in keep])
        data = load_site_settings(settings_path(settings))
        if url in data["ignored"]:
            data["ignored"].pop(url, None)
            path = settings_path(settings)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".site_settings.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
    return True
