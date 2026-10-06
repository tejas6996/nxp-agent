"""
Custom exception classes for the application.

Centralises error definitions so handlers in main.py can map them
to consistent HTTP responses without importing business logic.
"""


class AppBaseException(Exception):
    """Base class for all application-specific exceptions."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class FirecrawlClientError(AppBaseException):
    """Raised when the Firecrawl API returns an error or quota is exceeded."""


class OpenAIClientError(AppBaseException):
    """Raised when the OpenAI API returns an error."""


class StatePersistenceError(AppBaseException):
    """Raised when reading or writing scraper_state.json fails."""


class ArticleExtractionError(AppBaseException):
    """Raised when article content cannot be extracted from a URL."""


class DocumentBuildError(AppBaseException):
    """Raised when the digest document cannot be generated."""


class PipelineBusyError(AppBaseException):
    """Raised when another pipeline run already holds the state lock."""
