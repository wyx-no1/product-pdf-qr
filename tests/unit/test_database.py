"""Connection-pool lifecycle and readiness unit tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

import product_pdf_qr.database as database_module
from product_pdf_qr.config import Settings
from product_pdf_qr.database import Database

pytestmark = pytest.mark.anyio


class FakeCursor:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    async def fetchone(self) -> dict[str, object] | None:
        return self.row


class FakeConnection:
    def __init__(self, row: dict[str, object] | None, should_fail: bool = False) -> None:
        self.row = row
        self.should_fail = should_fail

    async def execute(self, _query: str) -> FakeCursor:
        if self.should_fail:
            raise RuntimeError("synthetic connection failure")
        return FakeCursor(self.row)


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.fake_connection = connection
        self.opened = False
        self.waited = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    async def wait(self) -> None:
        self.waited = True

    async def close(self) -> None:
        self.closed = True

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[FakeConnection]:
        yield self.fake_connection


def settings() -> Settings:
    return Settings.model_validate(
        {"database_url": "postgresql://app_rw:synthetic@127.0.0.1:5432/test"}
    )


def database_with_pool(
    monkeypatch: pytest.MonkeyPatch,
    pool: FakePool,
) -> Database:
    def fake_pool_factory(*_args: object, **_kwargs: object) -> FakePool:
        return pool

    monkeypatch.setattr(database_module, "AsyncConnectionPool", fake_pool_factory)
    return Database(settings())


async def test_pool_lifecycle_and_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool(FakeConnection({"ready": 1}))
    database = database_with_pool(monkeypatch, pool)

    await database.open()
    async with database.connection() as _connection:
        yielded_connections = 1
    await database.close()

    assert yielded_connections == 1
    assert pool.opened
    assert pool.waited
    assert pool.closed


async def test_readiness_success(monkeypatch: pytest.MonkeyPatch) -> None:
    database = database_with_pool(
        monkeypatch,
        FakePool(FakeConnection({"ready": 1})),
    )

    assert await database.is_ready()


@pytest.mark.parametrize(
    "connection",
    [
        FakeConnection(None),
        FakeConnection({"ready": 0}),
        FakeConnection(None, should_fail=True),
    ],
)
async def test_readiness_failure(
    monkeypatch: pytest.MonkeyPatch,
    connection: FakeConnection,
) -> None:
    database = database_with_pool(monkeypatch, FakePool(connection))

    assert not await database.is_ready()
