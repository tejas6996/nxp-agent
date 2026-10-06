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
    openai_model: str = Field(default="gpt-6-luna", alias="OPENAI_MODEL")
    # none | low | medium | high. Leave empty for models without reasoning support.
    openai_reasoning_effort: str = Field(default="low", alias="OPENAI_REASONING_EFFORT")
    max_concurrent_openai: int = Field(default=8, alias="MAX_CONCURRENT_OPENAI")
    # USD per 1M tokens, used to report each run's cost (gpt-6-luna list prices).
    openai_price_input_per_m: float = Field(default=0.10, alias="OPENAI_PRICE_INPUT_PER_M")
    openai_price_cached_input_per_m: float = Field(
        default=0.10, alias="OPENAI_PRICE_CACHED_INPUT_PER_M"
    )
    openai_price_output_per_m: float = Field(default=0.50, alias="OPENAI_PRICE_OUTPUT_PER_M")

    # Firecrawl (fallback only - used when a page cannot be fetched directly)
    firecrawl_api_key: str = Field(default="", alias="FIRECRAWL_API_KEY")
    firecrawl_enabled: bool = Field(default=True, alias="FIRECRAWL_ENABLED")
    # "basic" always costs 1 credit; "auto" may silently retry with a 5-credit proxy.
    firecrawl_proxy: str = Field(default="basic", alias="FIRECRAWL_PROXY")
    firecrawl_max_credits_per_run: int = Field(default=15, alias="FIRECRAWL_MAX_CREDITS_PER_RUN")
    firecrawl_credit_reserve: int = Field(default=50, alias="FIRECRAWL_CREDIT_RESERVE")
    firecrawl_runs_per_day: int = Field(default=4, alias="FIRECRAWL_RUNS_PER_DAY")
    firecrawl_listing_cooldown_hours: float = Field(
        default=8, alias="FIRECRAWL_LISTING_COOLDOWN_HOURS"
    )
    firecrawl_max_age_minutes: int = Field(default=60, alias="FIRECRAWL_MAX_AGE_MINUTES")
    max_concurrent_requests: int = Field(default=2, alias="MAX_CONCURRENT_REQUESTS")

    # Direct HTTP fetching (free)
    http_max_concurrent: int = Field(default=16, alias="HTTP_MAX_CONCURRENT")
    http_max_per_host: int = Field(default=2, alias="HTTP_MAX_PER_HOST")
    http_timeout_seconds: float = Field(default=25, alias="HTTP_TIMEOUT_SECONDS")

    # Scraper
    lookback_days: int = Field(default=7, alias="LOOKBACK_DAYS")
    max_articles_per_site: int = Field(default=20, alias="MAX_ARTICLES_PER_SITE")
    # How often a site that needed Firecrawl is re-tried with a free direct fetch.
    direct_recheck_days: int = Field(default=7, alias="DIRECT_RECHECK_DAYS")

    # Web UI: optional automatic runs while the UI server is running, e.g. "08:00,12:00,16:00,20:00"
    auto_run_times: str = Field(default="", alias="AUTO_RUN_TIMES")

    # Paths
    output_dir: str = Field(default="output_docs", alias="OUTPUT_DIR")
    state_file: str = Field(default="scraper_state.json", alias="STATE_FILE")


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings instance."""
    return Settings()
