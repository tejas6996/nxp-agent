"""
Tests for application settings.
"""

from app.settings import get_settings


def test_settings_loads() -> None:
    """Verify settings can be instantiated without errors."""
    settings = get_settings()
    assert settings.app_port == 8000
    assert settings.lookback_days == 7
