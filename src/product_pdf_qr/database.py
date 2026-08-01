"""PostgreSQL connection-pool lifecycle and readiness checks."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from product_pdf_qr.config import Settings

Connection = AsyncConnection[dict[str, object]]


class Database:
    """Own the runtime pool used exclusively with the least-privilege role."""

    def __init__(self, settings: Settings) -> None:
        self._pool: AsyncConnectionPool[Connection] = AsyncConnectionPool(
            conninfo=str(settings.database_url),
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            kwargs={"row_factory": dict_row},
            open=False,
        )

    async def open(self) -> None:
        """Open the pool and wait until its minimum connections are ready."""

        await self._pool.open()
        await self._pool.wait()

    async def close(self) -> None:
        """Close every pooled connection."""

        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection]:
        """Yield one pooled connection."""

        async with self._pool.connection() as connection:
            yield connection

    async def is_ready(self) -> bool:
        """Return whether PostgreSQL accepts a minimal runtime-role query."""

        try:
            async with self._pool.connection() as connection:
                result = await connection.execute("SELECT 1 AS ready")
                row = await result.fetchone()
                return row == {"ready": 1}
        except Exception:
            return False
