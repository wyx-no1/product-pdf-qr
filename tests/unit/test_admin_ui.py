"""Authenticated administration page contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from product_pdf_qr.domains.auth import SESSION_COOKIE_NAME, AuthenticatedAdmin
from product_pdf_qr.main import create_app

pytestmark = pytest.mark.anyio


def admin_identity(*, must_change_password: bool = False) -> AuthenticatedAdmin:
    return AuthenticatedAdmin(
        id=7,
        username="business-owner",
        must_change_password=must_change_password,
        session_id=UUID("11111111-1111-1111-1111-111111111111"),
        session_expires_at=datetime(2026, 8, 5, tzinfo=UTC),
    )


async def test_unauthenticated_admin_redirects_without_business_data() -> None:
    transport = httpx.ASGITransport(app=create_app())

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin")
        login = await client.get("/admin/login")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login?next=%2Fadmin"
    assert "产品列表" not in response.text
    assert login.status_code == 200
    assert "管理员登录" in login.text


@pytest.mark.parametrize("path", ["/admin", "/admin/products/7"])
async def test_authenticated_admin_pages_render(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(_database: object, _token: str) -> AuthenticatedAdmin:
        return admin_identity()

    monkeypatch.setattr("product_pdf_qr.auth_middleware.resolve_session", resolve)
    app = create_app()
    app.state.database = object()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: "browser-token"},
    ) as client:
        response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert 'id="create-form"' in response.text
    assert 'id="product-table"' in response.text
    assert 'id="detail-view"' in response.text
    assert "管理员：business-owner" in response.text  # noqa: RUF001


async def test_admin_page_uses_session_identity_for_complete_browser_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(_database: object, _token: str) -> AuthenticatedAdmin:
        return admin_identity()

    monkeypatch.setattr("product_pdf_qr.auth_middleware.resolve_session", resolve)
    app = create_app()
    app.state.database = object()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: "browser-token"},
    ) as client:
        response = await client.get("/admin")

    page = response.text
    assert 'name="code"' in page
    assert 'name="name"' in page
    assert 'name="file"' in page
    assert 'body.append("file", file)' in page
    assert "actor_id" not in page
    assert "ACTOR_ID" not in page
    assert "操作人 ID" not in page
    assert "managementFetch" in page
    assert "/api/products/${currentProduct.id}/pdf" in page
    assert 'name="q"' in page
    assert 'name="pdf_status"' in page
    assert '<option value="uploaded">已上传 PDF</option>' in page
    assert '<option value="not_uploaded">未上传 PDF</option>' in page
    assert "new URLSearchParams" in page
    assert 'parameters.set("q", query)' in page
    assert 'parameters.set("pdf_status", pdfStatusFilter.value)' in page
    assert "listOffset += LIST_PAGE_SIZE" in page
    assert "没有匹配的产品。请调整或清除搜索条件。" in page
    assert "清除条件" in page
    assert "managementFetch(`/api/products/${productId}`)" in page
    assert "未找到该产品，可能已被移除。" in page  # noqa: RUF001
    assert "暂无产品。请使用上方表单创建第一个产品。" in page


async def test_admin_page_routes_do_not_expand_openapi_surface() -> None:
    paths = create_app().openapi()["paths"]

    assert "/admin" not in paths
    assert "/admin/login" not in paths
    assert "/admin/change-password" not in paths
    assert "/admin/products/{product_id}" not in paths
