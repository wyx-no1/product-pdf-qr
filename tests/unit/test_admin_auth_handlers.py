"""Login, password-change, cookie, and logout handler tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from product_pdf_qr.config import Settings
from product_pdf_qr.domains.audit import AuditEvent
from product_pdf_qr.domains.auth import (
    SESSION_COOKIE_NAME,
    AuthenticatedAdmin,
    CreatedSession,
    LoginRateLimiter,
    csrf_token_for_session,
)
from product_pdf_qr.errors import AppError
from product_pdf_qr.main import create_app

pytestmark = pytest.mark.anyio


def identity(*, must_change_password: bool = True) -> AuthenticatedAdmin:
    return AuthenticatedAdmin(
        id=7,
        username="owner",
        must_change_password=must_change_password,
        session_id=UUID("11111111-1111-1111-1111-111111111111"),
        session_expires_at=datetime(2026, 8, 5, tzinfo=UTC),
    )


def configured_app() -> FastAPI:
    app = create_app()
    app.state.database = object()
    app.state.settings = Settings.model_validate(
        {
            "database_url": "postgresql://app_rw:synthetic@127.0.0.1:5432/test",
            "session_cookie_secure": False,
            "session_ttl_seconds": 3600,
        }
    )
    app.state.password_manager = object()
    app.state.login_rate_limiter = LoginRateLimiter(
        failure_limit=2,
        window_seconds=60,
        base_backoff_seconds=10,
        max_backoff_seconds=10,
    )
    return app


async def test_login_failure_and_dual_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_login(*_args: object, **_kwargs: object) -> None:
        return None

    audit_events: list[AuditEvent] = []

    async def audit(_database: object, event: AuditEvent) -> bool:
        audit_events.append(event)
        return True

    monkeypatch.setattr(
        "product_pdf_qr.admin.create_authenticated_session",
        failed_login,
    )
    monkeypatch.setattr("product_pdf_qr.admin.append_independent_event", audit)
    transport = httpx.ASGITransport(app=configured_app())

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login_page = await client.get("/admin/login?next=https://attacker.invalid")
        first = await client.post(
            "/admin/login",
            data={"username": "owner", "password": "wrong", "next": "/admin"},
        )
        second = await client.post(
            "/admin/login",
            data={"username": "owner", "password": "wrong", "next": "/admin"},
        )
        blocked = await client.post(
            "/admin/login",
            data={"username": "owner", "password": "wrong", "next": "/admin"},
        )

    assert login_page.status_code == 200
    assert 'value="/admin"' in login_page.text
    assert first.status_code == second.status_code == 401
    assert "用户名或密码错误" in first.text
    assert second.headers["retry-after"] == "10"
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "10"
    assert len(audit_events) == 3
    assert all(event.action == "login_failure" for event in audit_events)
    assert audit_events[-1].detail == {
        "username": "owner",
        "reason": "rate_limited",
    }


async def test_concurrent_login_burst_reserves_attempts_before_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_limit = 2
    burst_size = 8
    release_authentication = asyncio.Event()
    authentication_calls = 0

    async def failed_login(*_args: object, **_kwargs: object) -> None:
        nonlocal authentication_calls
        authentication_calls += 1
        await release_authentication.wait()
        return None

    async def audit(_database: object, _event: AuditEvent) -> bool:
        return True

    app = configured_app()
    app.state.login_rate_limiter = LoginRateLimiter(
        failure_limit=failure_limit,
        window_seconds=60,
        base_backoff_seconds=10,
        max_backoff_seconds=10,
    )
    monkeypatch.setattr(
        "product_pdf_qr.admin.create_authenticated_session",
        failed_login,
    )
    monkeypatch.setattr("product_pdf_qr.admin.append_independent_event", audit)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        tasks = [
            asyncio.create_task(
                client.post(
                    "/admin/login",
                    data={"username": "owner", "password": "wrong", "next": "/admin"},
                )
            )
            for _index in range(burst_size)
        ]
        burst = asyncio.gather(*tasks)
        for _iteration in range(100):
            await asyncio.sleep(0)
            completed_without_verification = sum(task.done() for task in tasks)
            if authentication_calls + completed_without_verification >= burst_size:
                break
        admitted_before_release = authentication_calls
        release_authentication.set()
        responses = await burst

    assert admitted_before_release == failure_limit
    assert sum(response.status_code == 401 for response in responses) == failure_limit
    assert sum(response.status_code == 429 for response in responses) == (
        burst_size - failure_limit
    )
    assert authentication_calls == failure_limit


async def test_successful_login_sets_reviewed_cookie_and_forces_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def successful_login(*_args: object, **_kwargs: object) -> CreatedSession:
        return CreatedSession(token="one-time-browser-token", admin=identity())

    monkeypatch.setattr(
        "product_pdf_qr.admin.create_authenticated_session",
        successful_login,
    )
    transport = httpx.ASGITransport(app=configured_app())

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/login",
            data={
                "username": "owner",
                "password": "TemporaryPassword-123",
                "next": "/admin/products/7",
            },
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/change-password"
    cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE_NAME}=one-time-browser-token" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=3600" in cookie
    assert "Secure" not in cookie


async def test_password_change_errors_success_and_logout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = identity()
    csrf_token = csrf_token_for_session("raw-token")

    async def resolve(_database: object, _token: str) -> AuthenticatedAdmin:
        return admin

    async def password_error(*_args: object, **_kwargs: object) -> None:
        raise AppError("invalid_current_password", "当前密码不正确。", 422)

    async def password_success(*_args: object, **_kwargs: object) -> None:
        return None

    revoked: list[int] = []

    async def revoke(_database: object, current: AuthenticatedAdmin) -> None:
        revoked.append(current.id)

    monkeypatch.setattr("product_pdf_qr.auth_middleware.resolve_session", resolve)
    monkeypatch.setattr("product_pdf_qr.admin.change_password", password_error)
    monkeypatch.setattr("product_pdf_qr.admin.revoke_session", revoke)
    transport = httpx.ASGITransport(app=configured_app())

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: "raw-token"},
    ) as client:
        page = await client.get("/admin/change-password")
        missing_csrf = await client.post(
            "/admin/change-password",
            data={
                "current_password": "CurrentPassword-123",
                "new_password": "NewPassword-456",
                "confirm_password": "NewPassword-456",
            },
        )
        invalid_csrf = await client.post(
            "/admin/change-password",
            data={
                "current_password": "CurrentPassword-123",
                "new_password": "NewPassword-456",
                "confirm_password": "NewPassword-456",
                "csrf_token": "invalid",
            },
        )
        mismatch = await client.post(
            "/admin/change-password",
            data={
                "current_password": "CurrentPassword-123",
                "new_password": "NewPassword-456",
                "confirm_password": "DifferentPassword-789",
                "csrf_token": csrf_token,
            },
        )
        invalid_current = await client.post(
            "/admin/change-password",
            data={
                "current_password": "wrong",
                "new_password": "NewPassword-456",
                "confirm_password": "NewPassword-456",
                "csrf_token": csrf_token,
            },
        )
        monkeypatch.setattr("product_pdf_qr.admin.change_password", password_success)
        changed = await client.post(
            "/admin/change-password",
            data={
                "current_password": "CurrentPassword-123",
                "new_password": "NewPassword-456",
                "confirm_password": "NewPassword-456",
                "csrf_token": csrf_token,
            },
        )
        invalid_logout = await client.post(
            "/admin/logout",
            data={"csrf_token": "invalid"},
        )
        logout = await client.post(
            "/admin/logout",
            data={"csrf_token": csrf_token},
        )

    assert page.status_code == 200
    assert "修改初始密码" in page.text
    assert f'value="{csrf_token}"' in page.text
    assert missing_csrf.status_code == invalid_csrf.status_code == 403
    assert invalid_logout.status_code == 403
    assert mismatch.status_code == 422
    assert "两次输入的新密码不一致" in mismatch.text
    assert invalid_current.status_code == 422
    assert "当前密码不正确" in invalid_current.text
    assert changed.status_code == 303
    assert changed.headers["location"] == "/admin"
    assert logout.status_code == 303
    assert logout.headers["location"] == "/admin/login"
    assert revoked == [7]
    assert "Max-Age=0" in logout.headers["set-cookie"]
