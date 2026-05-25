"""
API v1 router aggregator.

Registers all v1 endpoint routers onto a single prefix router
that main.py mounts at /api/v1.
"""

from fastapi import APIRouter

router = APIRouter()

from app.api.v1.run import router as run_router

router.include_router(run_router)
