"""
Pydantic domain models used across the application.

Models are grouped by domain. Only shared/cross-cutting models live here;
service-specific request/response schemas live alongside their routes.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel, HttpUrl


class ArticleListing(BaseModel):
    """Represents a single article entry returned from a listing page."""

    title: str
    url: str
    date: Optional[str] = None
    snippet: Optional[str] = None
    source_name: str


class ArticleContent(BaseModel):
    """Full extracted content for a single article."""

    title: str
    url: str
    published_date: Optional[str] = None
    image_url: Optional[str] = None
    summary: Optional[str] = None
    raw_content: Optional[str] = None
    source_name: str


class DigestRunResult(BaseModel):
    """Result of a single pipeline run."""

    run_date: date
    total_articles: int
    sources_processed: int
    document_path: Optional[str] = None
    errors: list[str] = []
