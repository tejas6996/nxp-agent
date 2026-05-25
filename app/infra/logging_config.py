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
