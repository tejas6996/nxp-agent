"""
Pipeline trigger endpoint.

GET /api/v1/run triggers the full news scraping and digest generation pipeline.
"""

import logging

from fastapi import APIRouter

from app.dependency import SettingsDep
from app.models import DigestRunResult
from app.services.create_doc import run_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pipeline"])


@router.get("/run", response_model=DigestRunResult)
async def trigger_pipeline(settings: SettingsDep) -> DigestRunResult:
    """Trigger the full news scraping and Word document generation pipeline."""
    logger.info("Pipeline trigger received via API.")
    return await run_pipeline(settings)
