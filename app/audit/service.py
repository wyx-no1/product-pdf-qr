import logging
import uuid
from typing import Any

from psycopg import AsyncConnection

from app.db import Pool

logger = logging.getLogger(__name__)


async def write_audit(
    conn: AsyncConnection[dict[str, Any]],
    *,
    action: str,
    result: str,
    actor_type: str = "system",
    actor_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    product_code: str | None = None,
    request_id: uuid.UUID | None = None,
    detail: dict[str, object] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO audit_events (
            actor_type, actor_id, action, target_type, target_id,
            product_code, result, request_id, detail
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            actor_type,
            actor_id,
            action,
            target_type,
            target_id,
            product_code,
            result,
            request_id,
            detail,
        ),
    )


async def write_failure_independently(pool: Pool, **event: object) -> None:
    try:
        async with pool.connection() as conn, conn.transaction():
            await write_audit(conn, **event)  # type: ignore[arg-type]
    except Exception:
        logger.exception("AUDIT_WRITE_FAILURE action=%s", event.get("action"))
