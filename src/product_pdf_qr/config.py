"""Centralized environment-backed application settings."""

import re
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, PostgresDsn, model_validator
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
PRODUCTION_PROXY_IP = "172.30.0.10"
PRODUCTION_APP_IP = "172.30.0.20"
_DNS_NAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z",
    re.ASCII,
)
_PLACEHOLDER_HOSTS = frozenset(
    {
        "domain.example",
        "example.com",
        "example.net",
        "example.org",
        "example.test",
        "your-domain.example",
    }
)


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deployment_mode: Literal["development", "production"] = "development"
    app_bind_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    forwarded_allow_ips: str = "127.0.0.1"
    database_url: PostgresDsn
    db_pool_min_size: int = Field(default=1, ge=1)
    db_pool_max_size: int = Field(default=5, ge=1)
    storage_root: Path = Path("storage/local")
    public_domain: str = ""
    public_base_url: str = "http://127.0.0.1:8000"
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
        """Reject inconsistent runtime and production-edge settings."""

        if self.db_pool_max_size < self.db_pool_min_size:
            raise ValueError("DB_POOL_MAX_SIZE must be at least DB_POOL_MIN_SIZE")
        if self.login_backoff_max_seconds < self.login_backoff_base_seconds:
            raise ValueError(
                "LOGIN_BACKOFF_MAX_SECONDS must be at least LOGIN_BACKOFF_BASE_SECONDS"
            )
        if self.deployment_mode == "production":
            self._validate_production_origin()
            if self.app_bind_host != PRODUCTION_APP_IP:
                raise ValueError("APP_BIND_HOST must be the fixed production frontend address")
            if self.forwarded_allow_ips != PRODUCTION_PROXY_IP:
                raise ValueError("FORWARDED_ALLOW_IPS must be the fixed production proxy address")
        return self

    def _validate_production_origin(self) -> None:
        """Require one exact HTTPS DNS origin shared with the production proxy."""

        if not self.public_domain or self.public_domain != self.public_domain.strip():
            raise ValueError("PUBLIC_DOMAIN must be a non-empty canonical DNS name")
        if (
            self.public_domain != self.public_domain.lower()
            or self.public_domain.endswith(".")
            or _DNS_NAME.fullmatch(self.public_domain) is None
            or self.public_domain in _PLACEHOLDER_HOSTS
            or self.public_domain.endswith(".invalid")
        ):
            raise ValueError("PUBLIC_DOMAIN must be a canonical non-placeholder DNS name")
        try:
            ip_address(self.public_domain)
        except ValueError:
            pass
        else:
            raise ValueError("PUBLIC_DOMAIN must not be an IP address")

        raw_url = self.public_base_url
        if raw_url != raw_url.strip() or any(ord(character) < 0x20 for character in raw_url):
            raise ValueError("PUBLIC_BASE_URL must not contain whitespace or controls")
        expected = f"https://{self.public_domain}"
        if raw_url != expected:
            raise ValueError(
                "PUBLIC_BASE_URL must exactly match https://PUBLIC_DOMAIN with no suffix"
            )
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != self.public_domain
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.path != ""
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("PUBLIC_BASE_URL must be an HTTPS DNS origin only")


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""

    return Settings()
