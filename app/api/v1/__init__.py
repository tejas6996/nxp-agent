"""
API v1 router aggregator.

Registers all v1 endpoint routers onto a single prefix router
that main.py mounts at /api/v1.
"""

from fastapi import APIRouter

from app.api.v1.run import router as run_router
from app.api.v1.ui import router as ui_router

router = APIRouter()
router.include_router(run_router)
router.include_router(ui_router)
