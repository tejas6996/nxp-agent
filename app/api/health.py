"""
Health check endpoint.

Mounted at the root level — no /api/v1 prefix.
"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict:
    """Return application liveness status."""
    return {"status": "ok"}
