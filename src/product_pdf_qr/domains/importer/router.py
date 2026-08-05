"""Authenticated Excel product-import API."""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse

from product_pdf_qr.config import Settings
from product_pdf_qr.database import Database
from product_pdf_qr.dependencies import get_current_admin, get_database, get_runtime_settings
from product_pdf_qr.domains.auth import AuthenticatedAdmin
from product_pdf_qr.domains.importer import ImportResult, import_products

router = APIRouter(prefix="/api/product-imports", tags=["product-imports"])


def _response_content(result: ImportResult) -> dict[str, object]:
    return {
        "status": result.status,
        "success_count": result.success_count,
        "duplicate_count": result.duplicate_count,
        "format_error_count": result.format_error_count,
        "errors": [
            {"row": error.row, "reason": error.reason, "kind": error.kind}
            for error in result.errors
        ],
        "notices": list(result.notices),
        "error_code": result.error_code,
    }


@router.post("")
async def import_products_endpoint(
    file: Annotated[UploadFile, File()],
    admin: Annotated[AuthenticatedAdmin, Depends(get_current_admin)],
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_runtime_settings)],
) -> JSONResponse:
    """Import one XLSX and return the same complete result rendered by the admin page."""

    result = await import_products(
        database,
        file,
        settings,
        actor_id=admin.id,
        request_id=uuid4(),
    )
    return JSONResponse(
        status_code=result.http_status,
        content=_response_content(result),
        headers={"Cache-Control": "no-store"},
    )
