"""Public HTTP contract: four 200 states, no-store, streaming, and miss limiting."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI

from product_pdf_qr.database import Database
from product_pdf_qr.domains.public import PublicMissLimiter
from product_pdf_qr.domains.storage import StorageService
from product_pdf_qr.main import create_app


class Cursor:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    async def fetchone(self) -> dict[str, object] | None:
        return self.row


class Connection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    async def execute(self, _query: str, _params: object = None) -> Cursor:
        return Cursor(self.row)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


class RepeatingDatabase:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection]:
        yield Connection(self.row)


def configured_app(
    tmp_path: Path,
    row: dict[str, object] | None,
    *,
    miss_limit: int = 20,
) -> tuple[FastAPI, StorageService]:
    app = create_app()
    storage = StorageService(tmp_path, max_pdf_bytes=1024)
    storage.prepare()
    app.state.database = cast(Database, RepeatingDatabase(row))
    app.state.storage_service = storage
    app.state.public_miss_limiter = PublicMissLimiter(miss_limit, 600)
    return app, storage


@pytest.mark.anyio
@pytest.mark.api
@pytest.mark.parametrize(
    ("row", "message"),
    [
        (None, "资料链接无效"),
        ({"status": "disabled", "current_version_id": None}, "该产品资料已停用"),
        ({"status": "active", "current_version_id": None}, "资料暂未上传"),
    ],
)
async def test_prompt_states_are_uniform_200_no_store(
    tmp_path: Path,
    row: dict[str, object] | None,
    message: str,
) -> None:
    app, _storage = configured_app(tmp_path, row)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/p/not-a-base32-token")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert message in response.text
    assert "A001" not in response.text
    assert "not-a-base32-token" not in response.text


@pytest.mark.anyio
@pytest.mark.api
async def test_available_state_streams_pdf_inline_without_caching(tmp_path: Path) -> None:
    relative = StorageService.relative_path_for_hash("d" * 64)
    row = {
        "status": "active",
        "current_version_id": 7,
        "original_filename": "合成 资料.pdf",
        "storage_path": relative,
        "size_bytes": 13,
    }
    app, storage = configured_app(tmp_path, row)
    path = storage.files_root / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-public!")
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/p/" + ("A" * 26))

    assert response.status_code == 200
    assert response.content == b"%PDF-public!"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith("inline; filename*=UTF-8''")


@pytest.mark.anyio
@pytest.mark.api
async def test_missing_token_threshold_audits_then_returns_429(tmp_path: Path) -> None:
    app, _storage = configured_app(tmp_path, None, miss_limit=1)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/p/missing")
        limited = await client.get("/p/another")

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "600"
    assert limited.headers["cache-control"] == "no-store"
