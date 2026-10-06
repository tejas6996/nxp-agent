"""
Pydantic domain models used across the application.

Models are grouped by domain. Only shared/cross-cutting models live here;
service-specific request/response schemas live alongside their routes.
"""

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

FetchMethod = Literal["feed", "direct", "firecrawl"]


class ArticleListing(BaseModel):
    """A single article entry discovered on a newsroom listing page or feed."""

    title: str
    url: str
    date: Optional[str] = None  # YYYY-MM-DD
    source_name: str
    source_url: str


class ArticleContent(BaseModel):
    """A fully processed article, as written to the digest PDF and JSON export.

    This is also the input format for downstream consumers (e.g. a future agent that
    drafts press releases), so `body_text` keeps the cleaned full article text.
    """

    title: str
    url: str
    source_name: str
    source_url: str
    published_date: Optional[str] = None  # YYYY-MM-DD
    image_url: Optional[str] = None
    summary: Optional[str] = None  # opening ~300 words of the article, verbatim
    body_text: Optional[str] = None  # cleaned article body (up to ~20k chars)
    fetched_via: Optional[FetchMethod] = None


class SiteReport(BaseModel):
    """Per-site outcome of a run (useful to spot broken or changed newsrooms)."""

    source_name: str
    url: str
    method: Optional[FetchMethod] = None
    articles_found: int = 0
    new_articles: int = 0
    status: Literal["ok", "unchanged", "skipped", "baselined", "failed"] = "ok"
    detail: Optional[str] = None


class ArticleIssue(BaseModel):
    """An article that made it into the digest without its content (title + link only)."""

    source_name: str
    title: str
    url: str
    problem: str


class DigestRunResult(BaseModel):
    """Result of a single pipeline run."""

    run_date: date
    run_at: Optional[datetime] = None
    total_articles: int
    sources_processed: int
    document_path: Optional[str] = None
    json_path: Optional[str] = None
    report_path: Optional[str] = None
    firecrawl_credits_used: int = 0
    openai_calls: int = 0
    openai_input_tokens: int = 0
    openai_cached_input_tokens: int = 0
    openai_output_tokens: int = 0
    openai_cost_usd: float = 0.0
    duration_seconds: Optional[float] = None
    trigger: Optional[str] = None  # cli | ui | scheduled | api
    failed_sources: list[str] = Field(default_factory=list)
    site_reports: list[SiteReport] = Field(default_factory=list)
    article_issues: list[ArticleIssue] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
