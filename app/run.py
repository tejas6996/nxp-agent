"""
Standalone pipeline runner.

Execute directly without the FastAPI server:
    uv run app/run.py
"""

import asyncio
import logging
import sys

from app.infra.logging_config import configure_logging
from app.services.create_doc import run_pipeline
from app.settings import get_settings


def main() -> None:
    """Entry point for the standalone pipeline runner."""
    settings = get_settings()
    configure_logging(settings.log_level)

    logger = logging.getLogger(__name__)
    logger.info("Starting news digest pipeline (standalone mode).")

    result = asyncio.run(run_pipeline(settings))

    if result.total_articles == 0:
        logger.info("No new articles found. Nothing to do.")
    else:
        logger.info(
            "Pipeline complete. %d articles from %d sources. Document: %s",
            result.total_articles,
            result.sources_processed,
            result.document_path,
        )

    if result.errors:
        logger.warning("Pipeline completed with errors: %s", result.errors)
        sys.exit(1)


if __name__ == "__main__":
    main()
