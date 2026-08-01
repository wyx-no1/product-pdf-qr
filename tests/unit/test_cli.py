"""Tests for the local-safe process entry point."""

import pytest

from product_pdf_qr import __main__
from product_pdf_qr.config import get_settings


def test_run_uses_centralized_loopback_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def fake_run(_app: object, *, host: str, port: int) -> None:
        calls.append((host, port))

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://app_rw:synthetic@127.0.0.1:5432/test",
    )
    monkeypatch.delenv("APP_BIND_HOST", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)
    monkeypatch.setattr("product_pdf_qr.__main__.uvicorn.run", fake_run)
    get_settings.cache_clear()
    try:
        __main__.run()
    finally:
        get_settings.cache_clear()

    assert calls == [("127.0.0.1", 8000)]
