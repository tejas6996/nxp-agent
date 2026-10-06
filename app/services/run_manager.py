"""
Background run management for the web UI.

Runs the pipeline as a background task inside the server process, captures its log
lines so the browser can show live progress, and optionally starts runs on a daily
schedule (AUTO_RUN_TIMES). The state-file lock still applies, so a UI run and a
command-line run can never overlap.
"""

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from app.exceptions import PipelineBusyError, StatePersistenceError
from app.models import DigestRunResult
from app.services.pipeline import run_pipeline
from app.settings import Settings

logger = logging.getLogger(__name__)

_MAX_LOG_LINES = 3000


class _CaptureHandler(logging.Handler):
    """Collects the pipeline's own log records (app.* loggers) into the run log."""

    def __init__(self, manager: "RunManager") -> None:
        super().__init__(level=logging.INFO)
        self._manager = manager
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        if not (record.name.startswith("app.") or record.name == "__main__"):
            return
        if record.name == __name__:
            return
        try:
            self._manager.add_log(self.format(record), record.levelname)
        except Exception:  # noqa: BLE001 - logging must never break the run
            pass


class RunManager:
    """Owns at most one in-process pipeline run at a time."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._log: deque[dict[str, Any]] = deque(maxlen=_MAX_LOG_LINES)
        self._seq = 0
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.trigger: str | None = None
        self.last_result: DigestRunResult | None = None
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def add_log(self, line: str, level: str = "INFO") -> None:
        self._seq += 1
        self._log.append({"seq": self._seq, "level": level, "line": line})

    def start(self, settings: Settings, trigger: str = "ui") -> bool:
        """Start a run in the background. Returns False if one is already running."""
        if self.running:
            return False
        self._log.clear()
        self.started_at = datetime.now().replace(microsecond=0)
        self.finished_at = None
        self.trigger = trigger
        self.last_error = None
        self.add_log(f"Run started ({trigger}) at {self.started_at:%Y-%m-%d %H:%M:%S}")
        self._task = asyncio.create_task(self._run(settings, trigger))
        return True

    async def _run(self, settings: Settings, trigger: str) -> None:
        handler = _CaptureHandler(self)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            self.last_result = await run_pipeline(settings, trigger=trigger)
            self.add_log(
                f"Run finished: {self.last_result.total_articles} new articles, "
                f"OpenAI ${self.last_result.openai_cost_usd:.4f}, "
                f"Firecrawl {self.last_result.firecrawl_credits_used} credits."
            )
        except PipelineBusyError as exc:
            self.last_error = exc.message
            self.add_log(exc.message, "ERROR")
        except StatePersistenceError as exc:
            self.last_error = exc.message
            self.add_log(exc.message, "ERROR")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Pipeline run crashed")
            self.last_error = f"Run failed: {exc}"
            self.add_log(self.last_error, "ERROR")
        finally:
            root.removeHandler(handler)
            self.finished_at = datetime.now().replace(microsecond=0)

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        """Current status plus log lines newer than `since` (a sequence number)."""
        return {
            "running": self.running,
            "trigger": self.trigger,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.last_error,
            "result": self.last_result.model_dump(mode="json") if self.last_result else None,
            "log": [entry for entry in self._log if entry["seq"] > since],
            "seq": self._seq,
        }


def parse_run_times(value: str) -> list[str]:
    """Parse "08:00, 12:30" into sorted valid HH:MM strings (invalid entries are dropped)."""
    times: set[str] = set()
    for part in value.split(","):
        part = part.strip()
        try:
            times.add(datetime.strptime(part, "%H:%M").strftime("%H:%M"))
        except ValueError:
            if part:
                logger.warning("Ignoring invalid AUTO_RUN_TIMES entry: %r", part)
    return sorted(times)


def next_scheduled_run(times: list[str], now: datetime | None = None) -> datetime | None:
    """The next datetime at which a scheduled run will start (None if no schedule)."""
    if not times:
        return None
    now = now or datetime.now()
    for t in times:
        candidate = datetime.combine(now.date(), datetime.strptime(t, "%H:%M").time())
        if candidate > now:
            return candidate
    tomorrow = now.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.strptime(times[0], "%H:%M").time())


async def scheduler_loop(manager: RunManager, settings: Settings) -> None:
    """Start a run at each AUTO_RUN_TIMES slot while the server is up."""
    times = parse_run_times(settings.auto_run_times)
    if not times:
        return
    logger.info("Automatic runs scheduled daily at: %s", ", ".join(times))
    fired: set[str] = set()
    while True:
        now = datetime.now()
        slot = now.strftime("%H:%M")
        key = f"{now:%Y-%m-%d} {slot}"
        if slot in times and key not in fired:
            fired.add(key)
            if manager.start(settings, trigger="scheduled"):
                logger.info("Scheduled run started (%s).", slot)
            else:
                logger.warning("Scheduled run at %s skipped: a run is already in progress.", slot)
        await asyncio.sleep(20)

