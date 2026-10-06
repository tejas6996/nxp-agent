"""
Run-state persistence (scraper_state.json) and the single-run lock.

State layout (version 2):
    {
      "version": 2,
      "last_run_date": "YYYY-MM-DD",
      "last_run_at": ISO timestamp,
      "seen_urls": {url_key: first_seen_date},
      "sites": {
        listing_url: {
          "baselined": bool,          # first successful scan done (its articles recorded)
          "method": "feed|direct|firecrawl",
          "method_checked": "YYYY-MM-DD",
          "last_links": [url_key, ...],  # links seen at the last LLM extraction
          "last_firecrawl_at": ISO timestamp,
          "last_success_at": ISO timestamp
        }
      }
    }

Version 1 files (seen_urls keyed by raw URL, no "sites") are migrated on load.
"""

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from app.exceptions import PipelineBusyError, StatePersistenceError
from app.services.parsing import url_key

logger = logging.getLogger(__name__)

STATE_VERSION = 2
_LOCK_STALE_SECONDS = 3 * 60 * 60


def _registrable_domain(url_or_key: str) -> str:
    host = urlparse(url_or_key if "://" in url_or_key else f"https://{url_or_key}").hostname or ""
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def load_state(state_path: Path, sites: list[dict[str, str]]) -> dict[str, Any]:
    """Load (and if needed migrate) run state.

    Returns an empty v2 state if the file does not exist.

    Raises:
        StatePersistenceError: If the file exists but cannot be read or parsed.
    """
    if not state_path.exists():
        logger.info("No state file at %s. Starting with empty state.", state_path)
        return {"version": STATE_VERSION, "seen_urls": {}, "sites": {}}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise StatePersistenceError(f"Failed to read state file {state_path}: {exc}") from exc

    if state.get("version", 1) < STATE_VERSION:
        state = _migrate_v1(state, sites)
    state.setdefault("seen_urls", {})
    state.setdefault("sites", {})
    return state


def _migrate_v1(old: dict[str, Any], sites: list[dict[str, str]]) -> dict[str, Any]:
    """Convert a v1 state file to v2.

    Seen URLs are re-keyed with url_key(). Sites whose domain already has seen URLs are
    marked as baselined so they keep producing digests; any other site gets a quiet
    baseline scan on its first run.
    """
    seen: dict[str, str] = {}
    for url, first_seen in old.get("seen_urls", {}).items():
        seen.setdefault(url_key(url), first_seen)
    seen_domains = {_registrable_domain(key) for key in seen}

    site_states: dict[str, dict[str, Any]] = {}
    for site in sites:
        if _registrable_domain(site["url"]) in seen_domains:
            site_states[site["url"]] = {"baselined": True}

    logger.info(
        "Migrated state file to v%d: %d seen URLs, %d sites already baselined.",
        STATE_VERSION,
        len(seen),
        len(site_states),
    )
    return {
        "version": STATE_VERSION,
        "last_run_date": old.get("last_run_date"),
        "seen_urls": seen,
        "sites": site_states,
    }


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    """Atomically persist run state (write to a temp file, then rename).

    An interrupted run can never leave a half-written, corrupt state file behind.

    Raises:
        StatePersistenceError: If the file cannot be written.
    """
    state["version"] = STATE_VERSION
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=state_path.name + ".", suffix=".tmp", dir=str(state_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            os.replace(tmp_name, state_path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        logger.info(
            "State saved to %s. Tracking %d URLs.", state_path, len(state.get("seen_urls", {}))
        )
    except OSError as exc:
        raise StatePersistenceError(f"Failed to write state file {state_path}: {exc}") from exc


@contextmanager
def run_lock(state_path: Path) -> Iterator[None]:
    """Prevent two pipeline runs from using the same state file at the same time.

    A lock older than three hours is treated as left over from a crashed run.

    Raises:
        PipelineBusyError: If another run currently holds the lock.
    """
    lock_path = state_path.with_name(state_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > _LOCK_STALE_SECONDS:
                logger.warning("Removing stale lock file %s (%.0f min old).", lock_path, age / 60)
                lock_path.unlink(missing_ok=True)
                continue
            raise PipelineBusyError(
                "Another digest run is already in progress (lock file "
                f"{lock_path}). Wait for it to finish and try again."
            ) from None
        with os.fdopen(fd, "w") as f:
            f.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        break
    else:
        raise PipelineBusyError(f"Could not acquire lock file {lock_path}.")

    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)
