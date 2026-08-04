"""Read-only formal-storage reconciliation."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from product_pdf_qr.database import Database
from product_pdf_qr.dependencies import get_database, get_storage_service
from product_pdf_qr.domains.storage import StorageService

router = APIRouter(prefix="/api/storage", tags=["storage"])


class OrphanFileResponse(BaseModel):
    """One read-only orphan report row."""

    storage_path: str
    size_bytes: int
    modified_at_ns: int


@router.get(
    "/orphans",
    response_model=list[OrphanFileResponse],
    summary="查看孤儿文件记录",
)
async def report_orphan_files(
    database: Annotated[Database, Depends(get_database)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
) -> list[OrphanFileResponse]:
    """List unreferenced files without changing either storage or the database."""

    async with database.connection() as connection:
        cursor = await connection.execute("SELECT storage_path FROM pdf_files")
        rows = await cursor.fetchall()
    referenced_paths = {str(row["storage_path"]) for row in rows}
    return [
        OrphanFileResponse(
            storage_path=orphan.storage_path,
            size_bytes=orphan.size_bytes,
            modified_at_ns=orphan.modified_at_ns,
        )
        for orphan in storage.find_orphans(referenced_paths)
    ]
