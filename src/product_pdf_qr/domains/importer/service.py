"""Two-phase Excel product import with atomic writes and durable failure audits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import UploadFile

from product_pdf_qr.config import Settings
from product_pdf_qr.database import Database
from product_pdf_qr.domains.audit import AuditEvent, append_event, append_independent_event
from product_pdf_qr.domains.importer.parser import (
    ParsedWorkbook,
    XlsxRejected,
    inspect_xlsx_container,
    parse_xlsx_with_timeout,
)
from product_pdf_qr.domains.product import (
    create_product_in_transaction,
    normalize_product_code,
    normalize_product_name,
)
from product_pdf_qr.errors import AppError

ImportErrorKind = Literal["format", "security", "system", "authentication"]
CODE_HEADERS = frozenset({"编码", "产品编码"})
NAME_HEADERS = frozenset({"名称", "产品名称"})
UPLOAD_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ImportRowError:
    """One complete, user-visible import error."""

    row: int | None
    reason: str
    kind: ImportErrorKind


@dataclass(frozen=True, slots=True)
class ImportCandidate:
    """A normalized first occurrence ready for phase-two insertion."""

    row: int
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Stable API/page result contract for every import outcome."""

    success_count: int
    duplicate_count: int
    format_error_count: int
    errors: tuple[ImportRowError, ...] = ()
    notices: tuple[str, ...] = ()
    status: Literal["success", "failure"] = "success"
    error_code: str | None = None
    http_status: int = 200


async def read_upload_with_limit(upload: UploadFile, max_bytes: int) -> bytes:
    """Read one upload incrementally and reject at the first byte over the limit."""

    content = bytearray()
    try:
        while True:
            remaining = max_bytes + 1 - len(content)
            chunk = await upload.read(min(UPLOAD_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > max_bytes:
                raise XlsxRejected(
                    "xlsx_too_large",
                    "文件超过 10 MB 上限。",
                    detail={
                        "reason": "upload_size_exceeded",
                        "actual_bytes": len(content),
                        "max_upload_bytes": max_bytes,
                    },
                    status_code=413,
                )
    finally:
        await upload.close()
    return bytes(content)


def _header_key(value: str) -> str:
    return value.strip().casefold()


def _cell(values: tuple[str, ...], index: int) -> str:
    return values[index] if index < len(values) else ""


def _code_error_reason(raw_code: str, error: AppError) -> str:
    stripped = raw_code.strip()
    if len(stripped) > 64:
        return "产品编码超过 64 字符。"
    if any(character.isspace() for character in stripped):
        return "产品编码含内部空格, 属于非法格式。"
    return f"产品编码含非法字符; {error.message}"


def validate_workbook(
    workbook: ParsedWorkbook,
    *,
    max_rows: int,
) -> tuple[tuple[ImportCandidate, ...], int, tuple[str, ...]]:
    """Validate all rows without persistence and retain first file occurrence."""

    nonblank_rows = tuple(
        row for row in workbook.rows if any(value.strip() for value in row.values)
    )
    if len(nonblank_rows) > max_rows:
        raise XlsxRejected(
            "xlsx_row_limit_exceeded",
            "数据行超过 5,000 行上限, 请分批导入。",
            detail={
                "reason": "row_limit_exceeded",
                "actual_rows": len(nonblank_rows),
                "max_rows": max_rows,
            },
            status_code=413,
        )

    normalized_headers = tuple(_header_key(header) for header in workbook.headers)
    code_columns = [
        index for index, header in enumerate(normalized_headers) if header in CODE_HEADERS
    ]
    if not code_columns:
        actual_headers = "、".join(repr(header) for header in workbook.headers)
        raise XlsxRejected(
            "missing_code_column",
            f"找不到编码列; 实际表头: {actual_headers}",
            detail={
                "reason": "missing_code_column",
                "actual_headers": list(workbook.headers),
            },
            format_error=True,
        )
    if len(code_columns) > 1:
        raise XlsxRejected(
            "ambiguous_code_column",
            "列名歧义: 编码列只能出现一次, 且“编码/产品编码”不能同时存在。",
            detail={
                "reason": "ambiguous_code_column",
                "actual_headers": list(workbook.headers),
            },
            format_error=True,
        )
    name_columns = [
        index for index, header in enumerate(normalized_headers) if header in NAME_HEADERS
    ]
    code_column = code_columns[0]
    name_column = name_columns[0] if name_columns else None
    candidates: list[ImportCandidate] = []
    seen_codes: set[str] = set()
    duplicate_count = 0
    errors: list[ImportRowError] = []

    for row in nonblank_rows:
        raw_code = _cell(row.values, code_column)
        if not raw_code.strip():
            errors.append(
                ImportRowError(
                    row=row.row_number,
                    reason="产品编码为空。",
                    kind="format",
                )
            )
            continue
        try:
            code = normalize_product_code(raw_code)
        except AppError as error:
            errors.append(
                ImportRowError(
                    row=row.row_number,
                    reason=_code_error_reason(raw_code, error),
                    kind="format",
                )
            )
            continue
        raw_name = _cell(row.values, name_column) if name_column is not None else ""
        try:
            name = normalize_product_name(raw_name) if raw_name.strip() else code
        except AppError:
            errors.append(
                ImportRowError(
                    row=row.row_number,
                    reason="产品名称超过 120 字符。",
                    kind="format",
                )
            )
            continue
        if code in seen_codes:
            duplicate_count += 1
            continue
        seen_codes.add(code)
        candidates.append(ImportCandidate(row=row.row_number, code=code, name=name))

    if errors:
        raise ImportFormatErrors(tuple(errors))
    notices = (
        (f"本文件含 {workbook.nonempty_sheet_count} 个工作表，仅导入第 1 个",)  # noqa: RUF001
        if workbook.nonempty_sheet_count > 1
        else ()
    )
    return tuple(candidates), duplicate_count, notices


class ImportFormatErrors(Exception):
    """All phase-one row errors, preserved without truncation."""

    def __init__(self, errors: tuple[ImportRowError, ...]) -> None:
        super().__init__("Import contains format errors")
        self.errors = errors


def _failure_result(
    *,
    code: str,
    message: str,
    kind: ImportErrorKind,
    http_status: int,
    format_error_count: int,
    errors: tuple[ImportRowError, ...] | None = None,
) -> ImportResult:
    return ImportResult(
        success_count=0,
        duplicate_count=0,
        format_error_count=format_error_count,
        errors=errors
        if errors is not None
        else (ImportRowError(row=None, reason=message, kind=kind),),
        status="failure",
        error_code=code,
        http_status=http_status,
    )


async def _audit_failure(
    database: Database,
    *,
    actor_id: int | None,
    request_id: UUID,
    result: ImportResult,
    detail: dict[str, object],
) -> None:
    await append_independent_event(
        database,
        AuditEvent(
            action="product_import",
            result="failure",
            actor_type="admin" if actor_id is not None else "anonymous",
            actor_id=actor_id,
            target_type="product_batch",
            request_id=request_id,
            detail={
                **detail,
                "success_count": result.success_count,
                "duplicate_count": result.duplicate_count,
                "format_error_count": result.format_error_count,
            },
        ),
    )


async def import_products(
    database: Database,
    upload: UploadFile,
    settings: Settings,
    *,
    actor_id: int,
    request_id: UUID,
) -> ImportResult:
    """Execute phase one fully, then commit phase two and success audit atomically."""

    try:
        content = await read_upload_with_limit(upload, settings.import_max_upload_bytes)
        if not upload.filename or not upload.filename.casefold().endswith(".xlsx"):
            raise XlsxRejected(
                "invalid_xlsx_extension",
                "请选择 .xlsx 文件。",
                detail={
                    "reason": "invalid_extension",
                    "actual_filename": upload.filename or "",
                },
                format_error=True,
            )
        metrics = inspect_xlsx_container(
            content,
            max_decompressed_bytes=settings.import_max_decompressed_bytes,
            max_compression_ratio=settings.import_max_compression_ratio,
        )
        workbook = await parse_xlsx_with_timeout(
            content,
            timeout_seconds=settings.import_parse_timeout_seconds,
        )
        candidates, file_duplicate_count, notices = validate_workbook(
            workbook,
            max_rows=settings.import_max_rows,
        )
    except ImportFormatErrors as failure:
        result = _failure_result(
            code="import_format_errors",
            message="文件包含格式错误。",
            kind="format",
            http_status=422,
            format_error_count=len(failure.errors),
            errors=failure.errors,
        )
        await _audit_failure(
            database,
            actor_id=actor_id,
            request_id=request_id,
            result=result,
            detail={"reason": "format_errors", "error_rows": len(failure.errors)},
        )
        return result
    except XlsxRejected as failure:
        format_error_count = 1 if failure.format_error else 0
        result = _failure_result(
            code=failure.code,
            message=failure.message,
            kind="format" if failure.format_error else "security",
            http_status=failure.status_code,
            format_error_count=format_error_count,
        )
        await _audit_failure(
            database,
            actor_id=actor_id,
            request_id=request_id,
            result=result,
            detail=failure.detail,
        )
        return result

    success_count = 0
    duplicate_count = file_duplicate_count
    try:
        async with database.connection() as connection:
            async with connection.transaction():
                for candidate in candidates:
                    try:
                        await create_product_in_transaction(
                            connection,
                            candidate.code,
                            candidate.name,
                            actor_id=actor_id,
                            request_id=request_id,
                        )
                    except AppError as error:
                        if error.code == "duplicate_product_code":
                            duplicate_count += 1
                            continue
                        raise
                    success_count += 1
                result = ImportResult(
                    success_count=success_count,
                    duplicate_count=duplicate_count,
                    format_error_count=0,
                    notices=notices,
                )
                await append_event(
                    connection,
                    AuditEvent(
                        action="product_import",
                        result="success",
                        actor_type="admin",
                        actor_id=actor_id,
                        target_type="product_batch",
                        request_id=request_id,
                        detail={
                            "success_count": success_count,
                            "duplicate_count": duplicate_count,
                            "format_error_count": 0,
                            "upload_bytes": len(content),
                            "decompressed_bytes": metrics.decompressed_bytes,
                            "compression_ratio": metrics.compression_ratio,
                        },
                    ),
                )
        return result
    except AppError as error:
        code = "token_retry_exhausted" if error.code == "token_generation_failed" else error.code
        message = (
            "系统错误: 公开标识 token 冲突重试耗尽, 整批未导入。"
            if error.code == "token_generation_failed"
            else f"系统错误: {error.message}"
        )
        result = _failure_result(
            code=code,
            message=message,
            kind="system",
            http_status=503,
            format_error_count=0,
        )
        await _audit_failure(
            database,
            actor_id=actor_id,
            request_id=request_id,
            result=result,
            detail={
                "reason": "system_error",
                "system_error_code": code,
                "attempted_success_count_before_rollback": success_count,
            },
        )
        return result
