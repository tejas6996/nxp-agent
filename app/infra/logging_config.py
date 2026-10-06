"""
Logging configuration for the application.

Call configure_logging() once at startup (in main.py or the CLI entrypoint)
before any loggers are used.
"""

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    """Configure root logger with a structured format.

    Args:
        level: Logging level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Windows consoles default to a legacy code page; headlines contain characters such
    # as ™, ® or CJK text that would otherwise raise encoding errors.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    file_handler = logging.FileHandler("scraper.log", encoding="utf-8")
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Avoid duplicate handlers if called more than once
    if not root_logger.handlers:
        root_logger.addHandler(handler)
        root_logger.addHandler(file_handler)

    # Third-party clients log every HTTP request at INFO; keep the log readable.
    for noisy in ("httpx", "httpcore", "openai", "firecrawl", "urllib3"):
        logging.getLogger(noisy).setLevel(max(log_level, logging.WARNING))
