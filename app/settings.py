"""
Application settings loaded from environment variables via pydantic-settings.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for the application.

    All values are read from environment variables or the .env file.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    app_env: str = Field(default="development", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # OpenAI
    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")

    # Firecrawl
    firecrawl_api_key: str = Field(alias="FIRECRAWL_API_KEY")

    # Scraper
    lookback_days: int = Field(default=7, alias="LOOKBACK_DAYS")
    max_articles_per_site: int = Field(default=20, alias="MAX_ARTICLES_PER_SITE")
    request_delay_seconds: float = Field(default=1.0, alias="REQUEST_DELAY_SECONDS")
    max_concurrent_requests: int = Field(default=5, alias="MAX_CONCURRENT_REQUESTS")

    # Paths
    output_dir: str = Field(default="output_docs", alias="OUTPUT_DIR")
    state_file: str = Field(default="scraper_state.json", alias="STATE_FILE")
    temp_images_dir: str = Field(default="temp_images", alias="TEMP_IMAGES_DIR")


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings instance."""
    return Settings()
