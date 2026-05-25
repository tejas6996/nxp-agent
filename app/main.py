"""
Application entry point.

Creates the FastAPI application instance, registers routers,
and attaches global exception handlers.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.v1 import router as v1_router
from app.exceptions import AppBaseException, FirecrawlClientError, OpenAIClientError
from app.infra.logging_config import configure_logging
from app.settings import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
configure_logging(settings.log_level)


def create_app() -> FastAPI:
    """Construct and configure the FastAPI application."""
    app = FastAPI(
        title="NXP News Agent",
        description="Automated semiconductor and tech news scraper with AI summarization",
        version="0.1.0",
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url="/redoc" if settings.app_env != "production" else None,
    )

    # Routers
    app.include_router(health_router)
    app.include_router(v1_router, prefix="/api/v1")

    # Exception handlers
    @app.exception_handler(FirecrawlClientError)
    async def firecrawl_error_handler(request: Request, exc: FirecrawlClientError) -> JSONResponse:
        logger.error("Firecrawl client error: %s", exc.message)
        return JSONResponse(status_code=502, content={"detail": exc.message})

    @app.exception_handler(OpenAIClientError)
    async def openai_error_handler(request: Request, exc: OpenAIClientError) -> JSONResponse:
        logger.error("OpenAI client error: %s", exc.message)
        return JSONResponse(status_code=502, content={"detail": exc.message})

    @app.exception_handler(AppBaseException)
    async def app_error_handler(request: Request, exc: AppBaseException) -> JSONResponse:
        logger.error("Application error: %s", exc.message)
        return JSONResponse(status_code=500, content={"detail": exc.message})

    return app


app = create_app()
