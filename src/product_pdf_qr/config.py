"""Centralized environment-backed application settings."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Self

from pydantic import Field, PostgresDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_bind_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    database_url: PostgresDsn
    db_pool_min_size: int = Field(default=1, ge=1)
    db_pool_max_size: int = Field(default=5, ge=1)
    storage_root: Path = Path("storage/local")

    def __init__(self, **values: Any) -> None:
        """Allow required values to be supplied by BaseSettings sources."""

        super().__init__(**values)

    @model_validator(mode="after")
    def validate_pool_bounds(self) -> Self:
        """Reject a pool whose maximum is smaller than its minimum."""

        if self.db_pool_max_size < self.db_pool_min_size:
            raise ValueError("DB_POOL_MAX_SIZE must be at least DB_POOL_MIN_SIZE")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""

    return Settings()
