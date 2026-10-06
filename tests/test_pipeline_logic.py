"""
Tests for state handling, credit budgeting, title verification and document output
(no network, no API keys used).
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.exceptions import PipelineBusyError
from app.infra.firecrawl import FirecrawlBudget
from app.models import ArticleContent, ArticleListing
from app.services.agent import _verified_title
from app.services.crawling import SiteScan
from app.services.create_doc import build_pdf, output_paths
from app.services.parsing import normalize_for_match, url_key
from app.services.pipeline import _select_new_articles
from app.services.state import load_state, run_lock, save_state
from app.settings import get_settings

SITES = [
    {"name": "Old", "url": "https://www.old.com/news"},
    {"name": "New", "url": "https://new.com/press"},
]


def test_v1_state_migration(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "last_run_date": "2026-06-23",
                "seen_urls": {"https://www.old.com:443/news/a?utm_source=x": "2026-06-01"},
            }
        ),
        encoding="utf-8",
    )
    state = load_state(path, SITES)
    assert state["version"] == 2
    assert url_key("https://old.com/news/a") in state["seen_urls"]
    assert state["sites"]["https://www.old.com/news"]["baselined"] is True
    assert "https://new.com/press" not in state["sites"]

    save_state(path, state)
    reloaded = load_state(path, SITES)
    assert reloaded["seen_urls"] == state["seen_urls"]


def test_run_lock_blocks_second_run(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    with run_lock(path):
        with pytest.raises(PipelineBusyError):
            with run_lock(path):
                pass
    # Released afterwards
    with run_lock(path):
        pass


def test_budget_allowance() -> None:
    settings = get_settings().model_copy(
        update={
            "firecrawl_max_credits_per_run": 15,
            "firecrawl_credit_reserve": 50,
            "firecrawl_runs_per_day": 4,
        }
    )
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    end = now + timedelta(days=23)
    # 950 spendable over 92 runs -> 10 per run
    assert FirecrawlBudget.compute_allowance(1000, end, settings, now) == 10
    # Never more than the cap
    assert FirecrawlBudget.compute_allowance(1000, now + timedelta(days=1), settings, now) == 15
    # Reserve is protected
    assert FirecrawlBudget.compute_allowance(40, end, settings, now) == 0
    # Unknown balance -> conservative
    assert FirecrawlBudget.compute_allowance(None, None, settings, now) == 5

    budget = FirecrawlBudget(2)
    assert budget.try_spend() and budget.try_spend()
    assert not budget.try_spend()
    assert budget.used == 2


def test_verified_title() -> None:
    page = normalize_for_match("AMD Sampling Versal™ AI Core Adaptive SoC in Space-Grade Package")
    # Exact (modulo trademark symbols / whitespace) -> accepted
    assert _verified_title("AMD Sampling Versal AI Core Adaptive SoC", page, "x") == (
        "AMD Sampling Versal AI Core Adaptive SoC"
    )
    # Reworded -> falls back to the page's own link text
    assert _verified_title("AMD ships space chip", page, "AMD Sampling Versal AI Core") == (
        "AMD Sampling Versal AI Core"
    )
    # Generic link text is never used as a title
    assert _verified_title("AMD ships space chip", page, "Read more") == "AMD ships space chip"


def _listing(url: str, d: str | None, site: dict[str, str]) -> ArticleListing:
    return ArticleListing(
        title=f"T {url}", url=url, date=d, source_name=site["name"], source_url=site["url"]
    )


def test_select_new_articles_baseline_lookback_and_dedupe() -> None:
    settings = get_settings().model_copy(update={"lookback_days": 7, "max_articles_per_site": 20})
    today = date(2026, 10, 6)
    old_site, new_site = SITES
    seen = {url_key("https://old.com/news/seen"): "2026-06-01"}
    site_states = {old_site["url"]: {"baselined": True}}
    scans = [
        SiteScan(
            site=old_site,
            status="ok",
            method="direct",
            articles=[
                _listing("https://www.old.com/news/seen/", "2026-10-05", old_site),  # seen
                _listing("https://old.com/news/fresh", "2026-10-05", old_site),  # new
                _listing("https://old.com/news/undated", None, old_site),  # new, no date
                _listing("https://old.com/news/ancient", "2026-01-01", old_site),  # too old
            ],
            link_fingerprint=["abc"],
        ),
        SiteScan(
            site=new_site,
            status="ok",
            method="direct",
            articles=[_listing("https://new.com/press/1", "2026-10-05", new_site)],
        ),
    ]
    selected, reports = _select_new_articles(
        scans, seen, site_states, settings, today, datetime(2026, 10, 6, 9, 0)
    )
    assert [a.url for a in selected] == ["https://old.com/news/fresh", "https://old.com/news/undated"]
    # Too-old article is recorded but not selected
    assert url_key("https://old.com/news/ancient") in seen
    # New site is baselined: recorded silently
    assert site_states[new_site["url"]]["baselined"] is True
    assert url_key("https://new.com/press/1") in seen
    assert reports[1].status == "baselined"
    assert site_states[old_site["url"]]["last_links"] == ["abc"]
    # Selected articles are NOT marked seen until the digest is written
    assert url_key("https://old.com/news/fresh") not in seen


def test_pdf_handles_ampersand_urls_and_unicode(tmp_path: Path) -> None:
    run_at = datetime(2026, 10, 6, 14, 30)
    pdf_path, json_path = output_paths(tmp_path, run_at)
    assert pdf_path.name == "News_Digest_06_10_2026_1430.pdf"
    article = ArticleContent(
        title="R&D <Update> — Versal™ 任命",
        url="https://example.com/a?x=1&y=2",
        source_name="Example & Co",
        source_url="https://example.com",
        published_date="2026-10-05",
        summary="First paragraph & more.\n\nSecond <paragraph>.",
    )
    build_pdf({"Example & Co": [article]}, {}, pdf_path, run_at)
    assert pdf_path.exists() and pdf_path.stat().st_size > 1000
    # A second run in the same minute gets a distinct name
    pdf2, _ = output_paths(tmp_path, run_at)
    assert pdf2.name == "News_Digest_06_10_2026_1430_2.pdf"


def test_strip_trailing_date() -> None:
    from app.services.agent import _strip_trailing_date

    assert _strip_trailing_date("Why architecture determines performance Oct 2, 2026") == (
        "Why architecture determines performance"
    )
    assert _strip_trailing_date("Chip launch news - 2026-10-02") == "Chip launch news"
    # Too short after stripping -> keep original
    assert _strip_trailing_date("News Oct 2, 2026") == "News Oct 2, 2026"
    assert _strip_trailing_date("Roadmap to 2030 and beyond") == "Roadmap to 2030 and beyond"


async def test_firecrawl_credits_go_to_longest_waiting_site() -> None:
    import asyncio

    from app.services.crawling import Crawler

    sites = [
        {"name": "Recent", "url": "https://recent.com/news"},
        {"name": "Never", "url": "https://never.com/news"},
        {"name": "Old", "url": "https://old.com/news"},
    ]
    site_states = {
        "https://recent.com/news": {"last_firecrawl_at": "2026-10-06T08:00:00"},
        "https://old.com/news": {"last_firecrawl_at": "2026-10-01T08:00:00"},
    }
    budget = FirecrawlBudget(2)
    crawler = Crawler(
        get_settings(), None, object(), budget, None, asyncio.Semaphore(1),  # type: ignore[arg-type]
        site_states, date(2026, 10, 6), datetime(2026, 10, 6, 12, 0),
    )
    served: list[str] = []

    async def free(site: dict[str, str]) -> SiteScan:
        return SiteScan(site=site, status="needs_firecrawl", detail="JS only")

    async def with_firecrawl(site, state, reasons):  # noqa: ANN001
        if not budget.try_spend():
            return SiteScan(site=site, status="skipped", detail="no credits")
        served.append(site["name"])
        await asyncio.sleep(0)
        return SiteScan(site=site, status="ok", method="firecrawl")

    crawler.scan_site_free = free  # type: ignore[method-assign]
    crawler._scan_with_firecrawl = with_firecrawl  # type: ignore[method-assign]
    scans = await crawler.scan_sites(sites)
    assert served == ["Never", "Old"]
    assert [s.status for s in scans] == ["skipped", "ok", "ok"]  # original order kept


def test_dedupe_sites() -> None:
    from app.services.pipeline import dedupe_sites

    sites = [
        {"name": "A", "url": "https://www.a.com/news/"},
        {"name": "A copy", "url": "http://a.com/news"},
        {"name": "a", "url": "https://other.com"},
        {"name": "B", "url": "https://b.com"},
    ]
    assert [s["name"] for s in dedupe_sites(sites)] == ["A", "B"]


def test_run_report_lists_failures(tmp_path: Path) -> None:
    from app.models import ArticleIssue, DigestRunResult, SiteReport
    from app.services.create_doc import write_run_report

    result = DigestRunResult(
        run_date=date(2026, 10, 6),
        run_at=datetime(2026, 10, 6, 9, 30),
        total_articles=1,
        sources_processed=1,
        site_reports=[
            SiteReport(source_name="Good", url="https://g.com", status="ok", method="direct"),
            SiteReport(source_name="Bad", url="https://b.com", status="failed", detail="HTTP 404"),
        ],
        article_issues=[
            ArticleIssue(source_name="Good", title="T", url="https://g.com/1", problem="blocked")
        ],
    )
    path = write_run_report(result, tmp_path)
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "FAILED SOURCES" in text and "HTTP 404" in text and "https://b.com" in text
    assert "https://g.com/1" in text and "blocked" in text


def test_schedule_parsing_and_next_run() -> None:
    from app.services.run_manager import next_scheduled_run, parse_run_times

    times = parse_run_times("16:00, 8:00,bad, 12:00 ,08:00")
    assert times == ["08:00", "12:00", "16:00"]
    assert next_scheduled_run(times, datetime(2026, 10, 7, 9, 30)) == datetime(2026, 10, 7, 12, 0)
    assert next_scheduled_run(times, datetime(2026, 10, 7, 17, 0)) == datetime(2026, 10, 8, 8, 0)
    assert next_scheduled_run([], datetime(2026, 10, 7, 9, 0)) is None


def test_token_usage_cost() -> None:
    from app.infra.openai import TokenUsage

    usage = TokenUsage(
        input_tokens=1_000_000, cached_input_tokens=200_000, output_tokens=100_000,
        price_input_per_m=0.10, price_cached_input_per_m=0.01, price_output_per_m=0.50,
    )
    assert usage.cost_usd == pytest.approx(0.08 + 0.002 + 0.05)
