"""
Standalone pipeline runner.

Execute directly without the FastAPI server:
    uv run app/run.py                     # normal run: build the digest
    uv run app/run.py --check Intel       # preview what is extracted from matching sites
    uv run app/run.py --check all         # preview every site (no state changes, no PDF)
"""

import argparse
import asyncio
import logging
import sys

from app.exceptions import PipelineBusyError, StatePersistenceError
from app.infra.logging_config import configure_logging
from app.services.pipeline import check_sites, run_pipeline
from app.settings import get_settings
from app.sites import select_sites


def _run_check(pattern: str, allow_firecrawl: bool) -> int:
    settings = get_settings()
    sites = select_sites(pattern)
    if not sites:
        print(f"No site matches {pattern!r}.")
        return 1
    scans, credits, usage = asyncio.run(check_sites(settings, sites, allow_firecrawl))
    for scan in scans:
        print(f"\n=== {scan.site['name']}  [{scan.status} via {scan.method or '-'}]  {scan.site['url']}")
        if scan.detail:
            print(f"    {scan.detail}")
        for a in scan.articles:
            print(f"  - {a.date or '----------'}  {a.title}\n                {a.url}")
    print(f"\nFirecrawl credits used: {credits}")
    print(f"OpenAI: {usage.calls} calls, ${usage.cost_usd:.4f}")
    return 0


def _run_press_release(url: str) -> int:
    from app.services.press_release import PressReleaseError, generate_press_release, to_markdown

    try:
        draft = asyncio.run(generate_press_release(url, get_settings()))
    except PressReleaseError as exc:
        print(f"Could not write the article: {exc.message}")
        return 1
    print(to_markdown(draft))
    print(f"Saved: output_docs/press_releases/{draft.id}.md  (OpenAI ${draft.openai_cost_usd:.4f})")
    return 0


def main() -> None:
    """Entry point for the standalone pipeline runner."""
    parser = argparse.ArgumentParser(description="NXP news digest pipeline")
    parser.add_argument(
        "--check",
        metavar="SITES",
        help="Preview extraction for sites whose name/URL contains any of these "
        "comma-separated words (or 'all'). Does not change state or build a PDF.",
    )
    parser.add_argument(
        "--press-release",
        metavar="URL",
        help="Write an EE Herald-style article from the press release at URL and print it.",
    )
    parser.add_argument(
        "--allow-firecrawl",
        action="store_true",
        help="With --check: allow Firecrawl (uses credits) for sites that need it.",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)

    if args.check:
        sys.exit(_run_check(args.check, args.allow_firecrawl))
    if args.press_release:
        sys.exit(_run_press_release(args.press_release))

    logger.info("Starting news digest pipeline (standalone mode).")
    try:
        result = asyncio.run(run_pipeline(settings, trigger="cli"))
    except PipelineBusyError as exc:
        logger.error(exc.message)
        sys.exit(2)
    except StatePersistenceError as exc:
        logger.error(exc.message)
        sys.exit(1)

    baselined = [r.source_name for r in result.site_reports if r.status == "baselined"]
    skipped = [f"{r.source_name}: {r.detail}" for r in result.site_reports if r.status == "skipped"]

    if result.total_articles == 0:
        logger.info("No new articles found. Nothing to do.")
    else:
        logger.info(
            "Pipeline complete. %d new articles. PDF: %s | JSON: %s",
            result.total_articles,
            result.document_path,
            result.json_path,
        )
    logger.info(
        "Sources OK: %d/%d | Firecrawl credits used: %d | OpenAI cost: $%.4f | %.0fs",
        result.sources_processed,
        len(result.site_reports),
        result.firecrawl_credits_used,
        result.openai_cost_usd,
        result.duration_seconds or 0,
    )
    if baselined:
        logger.info(
            "First scan of %d sources (their existing articles were recorded; new ones "
            "will appear from the next run): %s",
            len(baselined),
            ", ".join(baselined),
        )
    for line in skipped:
        logger.info("Skipped: %s", line)
    for line in result.failed_sources:
        logger.warning("Source failed: %s", line)
    for issue in result.article_issues:
        logger.warning("Article without content: %s (%s)", issue.url, issue.problem)
    if result.report_path:
        logger.info("Run report (all failures and skips): %s", result.report_path)

    if result.errors:
        logger.warning("Pipeline completed with errors: %s", result.errors)
        sys.exit(1)


if __name__ == "__main__":
    main()
