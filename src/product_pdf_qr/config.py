"""Centralized environment-backed application settings."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Self

from pydantic import AnyHttpUrl, Field, PostgresDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_PDF_VALIDATION_TIMEOUT_SECONDS = 5.0
DEFAULT_PDF_VALIDATION_CPU_SECONDS = 3
DEFAULT_PDF_VALIDATION_MEMORY_BYTES = 512 * 1024 * 1024
DEFAULT_SESSION_TTL_SECONDS = 12 * 60 * 60
DEFAULT_IMPORT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_IMPORT_MAX_DECOMPRESSED_BYTES = 50 * 1024 * 1024
DEFAULT_IMPORT_MAX_COMPRESSION_RATIO = 100.0
DEFAULT_IMPORT_PARSE_TIMEOUT_SECONDS = 30.0
DEFAULT_IMPORT_PARSE_MEMORY_BYTES = 512 * 1024 * 1024
DEFAULT_IMPORT_MAX_ROWS = 5_000


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
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000")
    max_pdf_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    import_max_upload_bytes: int = Field(default=DEFAULT_IMPORT_MAX_UPLOAD_BYTES, ge=1)
    import_max_decompressed_bytes: int = Field(
        default=DEFAULT_IMPORT_MAX_DECOMPRESSED_BYTES,
        ge=1,
    )
    import_max_compression_ratio: float = Field(
        default=DEFAULT_IMPORT_MAX_COMPRESSION_RATIO,
        gt=0,
    )
    import_parse_timeout_seconds: float = Field(
        default=DEFAULT_IMPORT_PARSE_TIMEOUT_SECONDS,
        gt=0,
    )
    import_parse_memory_bytes: int = Field(
        default=DEFAULT_IMPORT_PARSE_MEMORY_BYTES,
        ge=128 * 1024 * 1024,
    )
    import_max_rows: int = Field(default=DEFAULT_IMPORT_MAX_ROWS, ge=1)
    pdf_validation_timeout_seconds: float = Field(
        default=DEFAULT_PDF_VALIDATION_TIMEOUT_SECONDS,
        gt=0,
        le=30,
    )
    pdf_validation_cpu_seconds: int = Field(
        default=DEFAULT_PDF_VALIDATION_CPU_SECONDS,
        ge=1,
        le=10,
    )
    pdf_validation_memory_bytes: int = Field(
        default=DEFAULT_PDF_VALIDATION_MEMORY_BYTES,
        ge=128 * 1024 * 1024,
    )
    public_miss_limit: int = Field(default=20, ge=1)
    public_miss_window_seconds: int = Field(default=600, ge=1)
    session_cookie_secure: bool = True
    session_ttl_seconds: int = Field(default=DEFAULT_SESSION_TTL_SECONDS, ge=300)
    login_failure_limit: int = Field(default=5, ge=2)
    login_failure_window_seconds: int = Field(default=15 * 60, ge=60)
    login_backoff_base_seconds: float = Field(default=1.0, gt=0, le=60)
    login_backoff_max_seconds: float = Field(default=60.0, gt=0, le=600)

    def __init__(self, **values: Any) -> None:
        """Allow required values to be supplied by BaseSettings sources."""

        super().__init__(**values)

    @model_validator(mode="after")
    def validate_pool_bounds(self) -> Self:
        """Reject a pool whose maximum is smaller than its minimum."""

        if self.db_pool_max_size < self.db_pool_min_size:
            raise ValueError("DB_POOL_MAX_SIZE must be at least DB_POOL_MIN_SIZE")
        if self.login_backoff_max_seconds < self.login_backoff_base_seconds:
            raise ValueError(
                "LOGIN_BACKOFF_MAX_SECONDS must be at least LOGIN_BACKOFF_BASE_SECONDS"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""

    return Settings()
