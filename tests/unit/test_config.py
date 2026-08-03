"""Tests for centralized configuration defaults and validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from product_pdf_qr.config import Settings, get_settings


def test_settings_are_local_by_default() -> None:
    settings = Settings.model_validate(
        {"database_url": "postgresql://app_rw:synthetic@127.0.0.1:5432/test"}
    )

    assert settings.app_bind_host == "127.0.0.1"
    assert settings.app_port == 8000
    assert settings.storage_root == Path("storage/local")
    assert settings.max_pdf_bytes == 50 * 1024 * 1024
    assert settings.pdf_validation_timeout_seconds == 5
    assert settings.pdf_validation_cpu_seconds == 3
    assert settings.pdf_validation_memory_bytes == 512 * 1024 * 1024


def test_settings_reject_invalid_pool_bounds() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "database_url": "postgresql://app_rw:synthetic@127.0.0.1:5432/test",
                "db_pool_min_size": 0,
            }
        )


def test_settings_reject_maximum_below_minimum() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "database_url": "postgresql://app_rw:synthetic@127.0.0.1:5432/test",
                "db_pool_min_size": 3,
                "db_pool_max_size": 2,
            }
        )


def test_get_settings_loads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://app_rw:synthetic@127.0.0.1:5432/environment_test",
    )
    get_settings.cache_clear()
    try:
        assert get_settings().database_url.path == "/environment_test"
    finally:
        get_settings.cache_clear()
