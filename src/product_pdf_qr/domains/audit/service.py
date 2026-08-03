"""Append-only audit event writes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from psycopg.types.json import Jsonb

from product_pdf_qr.database import Connection, Database

logger = logging.getLogger(__name__)

AuditResult = Literal["success", "failure"]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """The safe, structured fields accepted by the audit table."""

    action: str
    result: AuditResult
    actor_type: str = "system"
    actor_id: int | None = None
    target_type: str | None = None
    target_id: int | None = None
    product_code: str | None = None
    request_id: UUID | None = None
    detail: dict[str, object] | None = None


async def append_event(connection: Connection, event: AuditEvent) -> None:
    """Append an event using the caller's transaction."""

    await connection.execute(
        """
        INSERT INTO audit_events (
            occurred_at,
            actor_type,
            actor_id,
            action,
            target_type,
            target_id,
            product_code,
            result,
            request_id,
            detail
        ) VALUES (
            now(), %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            event.actor_type,
            event.actor_id,
            event.action,
            event.target_type,
            event.target_id,
            event.product_code,
            event.result,
            event.request_id,
            Jsonb(event.detail) if event.detail is not None else None,
        ),
    )


async def append_independent_event(database: Database, event: AuditEvent) -> bool:
    """Append and commit an event independently from a failed business transaction."""

    try:
        async with database.connection() as connection:
            async with connection.transaction():
                await append_event(connection, event)
    except Exception:
        logger.exception("Independent audit event write failed", extra={"action": event.action})
        return False
    return True
