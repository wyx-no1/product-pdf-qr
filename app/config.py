from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    database_url: str = "postgresql://app_rw:local-only@127.0.0.1:5432/product_pdf_qr"
    bind_host: str = "127.0.0.1"
    port: int = 8000
    public_base_url: str = "http://127.0.0.1:8000"
    storage_root: Path = Path("storage/local")
    max_pdf_bytes: int = 50 * 1024 * 1024
    upload_chunk_bytes: int = 1024 * 1024
    database_pool_min_size: int = 1
    database_pool_max_size: int = 10
    log_level: str = "INFO"
    environment: str = Field(default="development", pattern="^(development|test)$")

    @field_validator("bind_host")
    @classmethod
    def reject_public_bind(cls, value: str) -> str:
        if value in {"0.0.0.0", "::"}:
            raise ValueError("Phase 1 must not bind to a public interface")
        return value

    @field_validator("public_base_url")
    @classmethod
    def strip_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def files_root(self) -> Path:
        return self.storage_root / "files"

    @property
    def temporary_root(self) -> Path:
        return self.storage_root / "tmp"

    @property
    def qrcode_root(self) -> Path:
        return self.storage_root / "qrcodes"

    def ensure_storage(self) -> None:
        for path in (self.files_root, self.temporary_root, self.qrcode_root):
            path.mkdir(parents=True, exist_ok=True)
        devices = {path.stat().st_dev for path in (self.files_root, self.temporary_root)}
        if len(devices) != 1:
            raise RuntimeError("temporary and permanent storage must share one filesystem")


@lru_cache
def get_settings() -> Settings:
    return Settings()
