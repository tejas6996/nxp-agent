"""
FastAPI dependency providers.

Each function here is intended to be used with FastAPI's Depends() mechanism.
"""

from typing import Annotated

from fastapi import Depends

from app.settings import Settings, get_settings

SettingsDep = Annotated[Settings, Depends(get_settings)]
