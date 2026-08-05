"""Serialized PDF upload and append-only version management."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from product_pdf_qr.database import Database
from product_pdf_qr.domains.audit import AuditEvent, append_event, append_independent_event
from product_pdf_qr.domains.storage import (
    PublishCancelled,
    PublishedFile,
    StorageService,
    ValidatedUpload,
)
from product_pdf_qr.errors import AppError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PDFVersion:
    """The version created by one successful upload."""

    id: int
    product_id: int
    version_no: int
    pdf_file_id: int
    sha256: str
    size_bytes: int
    storage_path: str
    original_filename: str


@dataclass(frozen=True, slots=True)
class PDFVersionHistoryItem:
    """One immutable version shown in the authenticated product history."""

    id: int
    product_id: int
    version_no: int
    original_filename: str
    uploaded_by: int
    uploaded_by_username: str
    uploaded_at: datetime
    is_current: bool


@dataclass(frozen=True, slots=True)
class RestoredPDFVersion:
    """The new product pointer after restoring an existing immutable version."""

    product_id: int
    version_id: int
    version_no: int


class DuplicateCurrentPDF(AppError):
    """The candidate is byte-identical to the locked current version."""

    def __init__(self) -> None:
        super().__init__("duplicate_current_pdf", "与当前文件相同", 409)


def _upload_rejection_event(
    *,
    product_id: int,
    actor_id: int,
    product_code: str | None,
    request_id: UUID | None,
    reason: str,
    sha256: str,
    published: PublishedFile | None,
) -> AuditEvent:
    detail: dict[str, object] = {
        "reason": reason,
        "stage": "locked_transaction",
        "sha256": sha256,
    }
    if published is not None and published.moved:
        detail["moved_file_sha256"] = sha256
    return AuditEvent(
        action="pdf_upload_rejected",
        result="failure",
        actor_type="admin",
        actor_id=actor_id,
        target_type="product",
        target_id=product_id,
        product_code=product_code,
        request_id=request_id,
        detail=detail,
    )


async def _append_shielded_rejection(database: Database, event: AuditEvent) -> None:
    """Wait for an independent rejection audit even while propagating cancellation."""

    audit_task = asyncio.create_task(append_independent_event(database, event))
    try:
        await asyncio.shield(audit_task)
    except asyncio.CancelledError:
        await audit_task


async def record_upload_rejection(
    database: Database,
    *,
    product_id: int,
    actor_id: int,
    reason: str,
    stage: str,
    request_id: UUID | None,
) -> bool:
    """Record a pre-transaction validation rejection on an independent connection."""

    return await append_independent_event(
        database,
        AuditEvent(
            action="pdf_upload_rejected",
            result="failure",
            actor_type="admin",
            actor_id=actor_id,
            target_type="product",
            target_id=product_id,
            request_id=request_id,
            detail={"reason": reason, "stage": stage},
        ),
    )


async def list_pdf_versions(
    database: Database,
    product_id: int,
) -> list[PDFVersionHistoryItem]:
    """Return all immutable versions and mark the product's current pointer."""

    async with database.connection() as connection:
        product_cursor = await connection.execute(
            "SELECT current_version_id FROM products WHERE id = %s",
            (product_id,),
        )
        product_row = await product_cursor.fetchone()
        if product_row is None:
            raise AppError("product_not_found", "产品不存在。", 404)
        cursor = await connection.execute(
            """
            SELECT
                v.id,
                v.product_id,
                v.version_no,
                v.original_filename,
                v.uploaded_by,
                a.username AS uploaded_by_username,
                v.uploaded_at,
                (v.id = p.current_version_id) AS is_current
            FROM pdf_versions AS v
            JOIN products AS p ON p.id = v.product_id
            JOIN admins AS a ON a.id = v.uploaded_by
            WHERE v.product_id = %s
            ORDER BY v.version_no DESC
            """,
            (product_id,),
        )
        rows = await cursor.fetchall()
    return [
        PDFVersionHistoryItem(
            id=cast(int, row["id"]),
            product_id=cast(int, row["product_id"]),
            version_no=cast(int, row["version_no"]),
            original_filename=str(row["original_filename"]),
            uploaded_by=cast(int, row["uploaded_by"]),
            uploaded_by_username=str(row["uploaded_by_username"]),
            uploaded_at=cast(datetime, row["uploaded_at"]),
            is_current=bool(row["is_current"]),
        )
        for row in rows
    ]


async def restore_pdf_version(
    database: Database,
    *,
    product_id: int,
    version_id: int,
    actor_id: int,
    request_id: UUID | None = None,
) -> RestoredPDFVersion:
    """Move only the current-version pointer to an existing version of this product."""

    async with database.connection() as connection:
        async with connection.transaction():
            product_cursor = await connection.execute(
                """
                SELECT id, code, current_version_id
                FROM products
                WHERE id = %s
                FOR UPDATE
                """,
                (product_id,),
            )
            product_row = await product_cursor.fetchone()
            if product_row is None:
                raise AppError("product_not_found", "产品不存在。", 404)

            version_cursor = await connection.execute(
                """
                SELECT
                    target.id,
                    target.version_no,
                    (
                        SELECT current.version_no
                        FROM pdf_versions AS current
                        WHERE current.product_id = %s
                          AND current.id = %s
                    ) AS from_version_no
                FROM pdf_versions AS target
                WHERE target.product_id = %s AND target.id = %s
                """,
                (
                    product_id,
                    product_row["current_version_id"],
                    product_id,
                    version_id,
                ),
            )
            version_row = await version_cursor.fetchone()
            if version_row is None:
                raise AppError(
                    "version_not_found_for_product",
                    "指定版本不存在或不属于该产品。",
                    404,
                )
            if version_row["from_version_no"] is None:
                raise RuntimeError("Current version pointer cannot be resolved")

            await connection.execute(
                """
                UPDATE products
                SET current_version_id = %s, updated_at = now()
                WHERE id = %s
                """,
                (version_id, product_id),
            )
            version_no = cast(int, version_row["version_no"])
            from_version_no = cast(int, version_row["from_version_no"])
            await append_event(
                connection,
                AuditEvent(
                    action="version_restore",
                    result="success",
                    actor_type="admin",
                    actor_id=actor_id,
                    target_type="version",
                    target_id=version_id,
                    product_code=str(product_row["code"]),
                    request_id=request_id,
                    detail={
                        "from_version_no": from_version_no,
                        "to_version_no": version_no,
                    },
                ),
            )
    return RestoredPDFVersion(
        product_id=product_id,
        version_id=version_id,
        version_no=version_no,
    )


async def upload_pdf(
    database: Database,
    storage: StorageService,
    *,
    product_id: int,
    actor_id: int,
    upload: ValidatedUpload,
    request_id: UUID | None = None,
) -> PDFVersion:
    """Lock, re-read current content, publish, append, repoint, and commit."""

    published: PublishedFile | None = None
    product_code: str | None = None
    try:
        async with database.connection() as connection:
            async with connection.transaction():
                product_cursor = await connection.execute(
                    """
                    SELECT id, code, current_version_id
                    FROM products
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (product_id,),
                )
                product_row = await product_cursor.fetchone()
                if product_row is None:
                    raise AppError("product_not_found", "产品不存在。", 404)
                product_code = str(product_row["code"])

                actor_cursor = await connection.execute(
                    "SELECT id FROM admins WHERE id = %s",
                    (actor_id,),
                )
                if await actor_cursor.fetchone() is None:
                    raise AppError("upload_actor_not_found", "上传操作者不存在。", 422)

                current_version_id = product_row["current_version_id"]
                if current_version_id is not None:
                    current_cursor = await connection.execute(
                        """
                        SELECT f.size_bytes, f.sha256
                        FROM pdf_versions AS v
                        JOIN pdf_files AS f ON f.id = v.pdf_file_id
                        WHERE v.product_id = %s AND v.id = %s
                        """,
                        (product_id, current_version_id),
                    )
                    current_row = await current_cursor.fetchone()
                    if current_row is None:
                        raise RuntimeError("Current version pointer cannot be resolved")
                    if (
                        cast(int, current_row["size_bytes"]) == upload.size_bytes
                        and str(current_row["sha256"]) == upload.sha256
                    ):
                        raise DuplicateCurrentPDF()

                version_cursor = await connection.execute(
                    """
                    SELECT COALESCE(MAX(version_no), 0) + 1 AS next_version_no
                    FROM pdf_versions
                    WHERE product_id = %s
                    """,
                    (product_id,),
                )
                version_row = await version_cursor.fetchone()
                if version_row is None:
                    raise RuntimeError("Version number query returned no row")
                next_version_no = cast(int, version_row["next_version_no"])

                published = await storage.publish(upload)

                file_cursor = await connection.execute(
                    """
                    INSERT INTO pdf_files (
                        sha256,
                        size_bytes,
                        storage_path,
                        created_at
                    ) VALUES (%s, %s, %s, now())
                    ON CONFLICT (sha256) DO NOTHING
                    RETURNING id, size_bytes, storage_path
                    """,
                    (upload.sha256, upload.size_bytes, published.storage_path),
                )
                file_row = await file_cursor.fetchone()
                if file_row is None:
                    existing_cursor = await connection.execute(
                        """
                        SELECT id, size_bytes, storage_path
                        FROM pdf_files
                        WHERE sha256 = %s
                        """,
                        (upload.sha256,),
                    )
                    file_row = await existing_cursor.fetchone()
                if file_row is None:
                    raise RuntimeError("Content-addressed file row cannot be resolved")
                if (
                    cast(int, file_row["size_bytes"]) != upload.size_bytes
                    or str(file_row["storage_path"]) != published.storage_path
                ):
                    raise RuntimeError("Content-addressed file metadata conflict")
                pdf_file_id = cast(int, file_row["id"])

                inserted_version = await connection.execute(
                    """
                    INSERT INTO pdf_versions (
                        product_id,
                        pdf_file_id,
                        version_no,
                        original_filename,
                        uploaded_by,
                        uploaded_at
                    ) VALUES (%s, %s, %s, %s, %s, now())
                    RETURNING id
                    """,
                    (
                        product_id,
                        pdf_file_id,
                        next_version_no,
                        upload.original_filename,
                        actor_id,
                    ),
                )
                inserted_row = await inserted_version.fetchone()
                if inserted_row is None:
                    raise RuntimeError("Version insert returned no row")
                version_id = cast(int, inserted_row["id"])

                await connection.execute(
                    """
                    UPDATE products
                    SET current_version_id = %s, updated_at = now()
                    WHERE id = %s
                    """,
                    (version_id, product_id),
                )
                await append_event(
                    connection,
                    AuditEvent(
                        action="pdf_upload",
                        result="success",
                        actor_type="admin",
                        actor_id=actor_id,
                        target_type="version",
                        target_id=version_id,
                        product_code=product_code,
                        request_id=request_id,
                        detail={
                            "version_no": next_version_no,
                            "sha256": upload.sha256,
                            "size_bytes": upload.size_bytes,
                        },
                    ),
                )
        return PDFVersion(
            id=version_id,
            product_id=product_id,
            version_no=next_version_no,
            pdf_file_id=pdf_file_id,
            sha256=upload.sha256,
            size_bytes=upload.size_bytes,
            storage_path=published.storage_path,
            original_filename=upload.original_filename,
        )
    except asyncio.CancelledError as error:
        if isinstance(error, PublishCancelled):
            published = error.published
        await _append_shielded_rejection(
            database,
            _upload_rejection_event(
                product_id=product_id,
                actor_id=actor_id,
                product_code=product_code,
                request_id=request_id,
                reason="upload_cancelled",
                sha256=upload.sha256,
                published=published,
            ),
        )
        raise
    except Exception as error:
        await append_independent_event(
            database,
            _upload_rejection_event(
                product_id=product_id,
                actor_id=actor_id,
                product_code=product_code,
                request_id=request_id,
                reason=error.code if isinstance(error, AppError) else "upload_transaction_failed",
                sha256=upload.sha256,
                published=published,
            ),
        )
        if isinstance(error, AppError):
            raise
        logger.exception("PDF upload transaction failed")
        raise AppError("pdf_upload_failed", "PDF 上传失败, 请重试。", 500) from error
    finally:
        upload.discard()
