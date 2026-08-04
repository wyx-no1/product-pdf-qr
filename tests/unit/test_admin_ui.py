"""Minimal administration page contract tests."""

from __future__ import annotations

import httpx
import pytest

from product_pdf_qr.main import create_app

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("path", ["/admin", "/admin/products/7"])
async def test_admin_pages_are_accessible_without_database(path: str) -> None:
    transport = httpx.ASGITransport(app=create_app())

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="create-form"' in response.text
    assert 'id="detail-view"' in response.text


async def test_admin_page_contains_complete_browser_workflow() -> None:
    transport = httpx.ASGITransport(app=create_app())

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin")

    page = response.text
    assert 'name="code"' in page
    assert 'name="name"' in page
    assert 'name="file"' in page
    assert "操作人 ID" in page
    assert "（第一期固定值）" in page  # noqa: RUF001
    assert 'fetch("/api/products"' in page
    assert 'body.append("actor_id", ACTOR_ID)' in page
    assert 'body.append("file", file)' in page
    assert "/api/products/${currentProduct.id}/pdf" in page
    assert "product.qrcode_url" in page
    assert "当前页面没有产品数据，请先创建产品。" in page  # noqa: RUF001


async def test_admin_page_routes_do_not_expand_openapi_surface() -> None:
    paths = create_app().openapi()["paths"]

    assert "/admin" not in paths
    assert "/admin/products/{product_id}" not in paths
