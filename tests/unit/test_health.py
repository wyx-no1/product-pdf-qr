"""Infrastructure endpoint and shared-error skeleton tests."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from product_pdf_qr.errors import AppError
from product_pdf_qr.main import create_app, lifespan

pytestmark = pytest.mark.anyio


class StubDatabase:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    async def is_ready(self) -> bool:
        return self.ready


@pytest.mark.parametrize(
    ("ready", "expected_status", "expected_body"),
    [
        (True, 200, {"status": "ok"}),
        (False, 503, {"status": "unavailable"}),
    ],
)
async def test_readiness_reflects_database_state(
    ready: bool,
    expected_status: int,
    expected_body: dict[str, str],
) -> None:
    app = create_app()
    app.state.database = StubDatabase(ready)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == expected_status
    assert response.json() == expected_body


async def test_liveness_has_no_business_dependency() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_shared_application_error_response() -> None:
    app = create_app()

    @app.get("/synthetic-error")
    async def synthetic_error() -> None:
        raise AppError("synthetic_error", "Synthetic safe message.", 409)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/synthetic-error")

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "synthetic_error",
            "message": "Synthetic safe message.",
        }
    }


async def test_unexpected_errors_do_not_leak_details() -> None:
    app = create_app()

    @app.get("/unexpected-error")
    async def unexpected_error() -> None:
        raise RuntimeError("sensitive synthetic detail")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/unexpected-error")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "An unexpected error occurred.",
        }
    }
    assert "sensitive" not in response.text


async def test_lifespan_opens_and_closes_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class StubLifecycleDatabase:
        def __init__(self, _settings: object) -> None:
            events.append("created")

        async def open(self) -> None:
            events.append("opened")

        async def close(self) -> None:
            events.append("closed")

    monkeypatch.setattr(
        "product_pdf_qr.main.Database",
        StubLifecycleDatabase,
    )
    app = FastAPI()
    async with lifespan(app):
        assert events == ["created", "opened"]
        assert isinstance(app.state.database, StubLifecycleDatabase)

    assert events == ["created", "opened", "closed"]
