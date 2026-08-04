from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg import AsyncConnection, AsyncCursor, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import Settings

Pool = AsyncConnectionPool[AsyncConnection[dict[str, Any]]]


def create_pool(settings: Settings) -> Pool:
    return AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.database_pool_min_size,
        max_size=settings.database_pool_max_size,
        open=False,
        kwargs={"autocommit": False, "row_factory": dict_row},
    )


@asynccontextmanager
async def connection(pool: Pool) -> AsyncIterator[AsyncConnection[dict[str, Any]]]:
    async with pool.connection() as conn:
        yield conn


async def fetch_one(
    cursor: AsyncCursor[dict[str, Any]],
    query: sql.Composable | str,
    parameters: tuple[object, ...] = (),
) -> dict[str, Any] | None:
    await cursor.execute(query, parameters)
    return await cursor.fetchone()
