"""Read-only QR generation failure reconciliation."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from product_pdf_qr.database import Database
from product_pdf_qr.dependencies import get_database, get_qrcode_service
from product_pdf_qr.domains.qrcode import QRCodeService

router = APIRouter(prefix="/api/qrcode", tags=["qrcode"])


class QRCodeFailureResponse(BaseModel):
    """One product whose last observed generation failure has no cache artifact."""

    product_id: int
    product_code: str
    reason: str | None


@router.get(
    "/failures",
    response_model=list[QRCodeFailureResponse],
    summary="查看二维码失败记录",
)
async def report_qrcode_failures(
    database: Annotated[Database, Depends(get_database)],
    qrcode_service: Annotated[QRCodeService, Depends(get_qrcode_service)],
) -> list[QRCodeFailureResponse]:
    """List generation failures without creating, changing, or deleting any QR image."""

    async with database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT DISTINCT ON (target_id)
                target_id,
                product_code,
                detail
            FROM audit_events
            WHERE action = 'qrcode_generation_failure'
              AND target_type = 'product'
              AND target_id IS NOT NULL
              AND product_code IS NOT NULL
            ORDER BY target_id, occurred_at DESC, id DESC
            """
        )
        rows = await cursor.fetchall()
    failures: list[QRCodeFailureResponse] = []
    for row in rows:
        product_code = str(row["product_code"])
        if (qrcode_service.cache_root / f"{product_code}.png").is_file():
            continue
        detail = row["detail"]
        reason = str(detail.get("reason")) if isinstance(detail, dict) else None
        failures.append(
            QRCodeFailureResponse(
                product_id=cast(int, row["target_id"]),
                product_code=product_code,
                reason=reason,
            )
        )
    return failures
