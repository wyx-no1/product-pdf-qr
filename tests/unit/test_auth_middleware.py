"""Uniform management authentication and public-route isolation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from product_pdf_qr.auth_middleware import AdminAuthenticationMiddleware
from product_pdf_qr.domains.auth import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    AuthenticatedAdmin,
    csrf_token_for_session,
)

pytestmark = pytest.mark.anyio


def build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AdminAuthenticationMiddleware)
    app.state.database = object()

    @app.get("/admin/private")
    async def admin_private() -> dict[str, str]:
        return {"secret": "management-data"}

    @app.get("/api/private")
    async def api_private() -> dict[str, str]:
        return {"secret": "api-data"}

    @app.post("/api/private")
    async def mutate_api_private() -> dict[str, str]:
        return {"result": "mutated"}

    @app.get("/admin/change-password")
    async def change_password_page() -> dict[str, str]:
        return {"allowed": "change-password"}

    @app.post("/admin/logout")
    async def logout() -> dict[str, str]:
        return {"allowed": "logout"}

    @app.get("/p/{public_token}")
    async def public_document(public_token: str) -> dict[str, str]:
        return {"public_token": public_token}

    @app.get("/health/ready")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def identity(*, must_change_password: bool) -> AuthenticatedAdmin:
    return AuthenticatedAdmin(
        id=7,
        username="owner",
        must_change_password=must_change_password,
        session_id=UUID("11111111-1111-1111-1111-111111111111"),
        session_expires_at=datetime(2026, 8, 5, tzinfo=UTC),
    )


async def test_unauthenticated_management_redirects_without_data() -> None:
    transport = httpx.ASGITransport(app=build_app())

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await client.get("/admin/private")
        api = await client.get("/api/private")

    assert admin.status_code == api.status_code == 303
    assert admin.headers["location"].startswith("/admin/login")
    assert api.headers["location"].startswith("/admin/login")
    assert "management-data" not in admin.text
    assert "api-data" not in api.text
    assert SESSION_COOKIE_NAME in admin.headers["set-cookie"]


async def test_force_change_is_enforced_in_middleware_for_direct_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(_database: object, _token: str) -> AuthenticatedAdmin:
        return identity(must_change_password=True)

    monkeypatch.setattr("product_pdf_qr.auth_middleware.resolve_session", resolve)
    transport = httpx.ASGITransport(app=build_app())

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: "raw-token"},
    ) as client:
        api = await client.get("/api/private")
        change = await client.get("/admin/change-password")
        logout = await client.post("/admin/logout")

    assert api.status_code == 303
    assert api.headers["location"] == "/admin/change-password"
    assert "api-data" not in api.text
    assert change.status_code == 200
    assert logout.status_code == 200


async def test_valid_changed_password_session_reaches_management(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(_database: object, _token: str) -> AuthenticatedAdmin:
        return identity(must_change_password=False)

    monkeypatch.setattr("product_pdf_qr.auth_middleware.resolve_session", resolve)
    transport = httpx.ASGITransport(app=build_app())

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: "raw-token"},
    ) as client:
        response = await client.get("/api/private")

    assert response.status_code == 200
    assert response.json() == {"secret": "api-data"}


async def test_api_mutation_requires_session_bound_csrf_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(_database: object, _token: str) -> AuthenticatedAdmin:
        return identity(must_change_password=False)

    monkeypatch.setattr("product_pdf_qr.auth_middleware.resolve_session", resolve)
    transport = httpx.ASGITransport(app=build_app())
    raw_token = "raw-token"

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: raw_token},
    ) as client:
        missing = await client.post("/api/private")
        invalid = await client.post(
            "/api/private",
            headers={CSRF_HEADER_NAME: csrf_token_for_session("other-token")},
        )
        valid = await client.post(
            "/api/private",
            headers={CSRF_HEADER_NAME: csrf_token_for_session(raw_token)},
        )

    assert missing.status_code == invalid.status_code == 403
    assert missing.json()["error"]["code"] == "invalid_csrf_token"
    assert valid.status_code == 200
    assert valid.json() == {"result": "mutated"}


async def test_public_product_path_and_health_are_always_anonymous() -> None:
    app = build_app()
    del app.state.database
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        public = await client.get("/p/PUBLICTOKEN")
        health = await client.get("/health/ready")

    assert public.status_code == 200
    assert public.json() == {"public_token": "PUBLICTOKEN"}
    assert health.status_code == 200
