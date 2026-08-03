"""Uniform-200 public scan endpoint."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from product_pdf_qr.database import Database
from product_pdf_qr.dependencies import (
    get_database,
    get_public_miss_limiter,
    get_storage_service,
)
from product_pdf_qr.domains.audit import AuditEvent, append_independent_event
from product_pdf_qr.domains.public.service import PublicMissLimiter, resolve_public_document
from product_pdf_qr.domains.storage import StorageService

router = APIRouter(tags=["public"])

NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}

STATUS_MESSAGES = {
    "missing": "资料链接无效",
    "disabled": "该产品资料已停用",
    "unuploaded": "资料暂未上传",
}


def _status_page(message: str) -> HTMLResponse:
    body = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>产品资料</title></head><body><main><p>"
        f"{message}"
        "</p></main></body></html>"
    )
    return HTMLResponse(body, status_code=200, headers=NO_STORE_HEADERS)


def _stream_file(path: object) -> Iterator[bytes]:
    from pathlib import Path

    with Path(str(path)).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            yield chunk


@router.get("/p/{public_token:path}", response_model=None)
async def public_scan(
    public_token: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    limiter: Annotated[PublicMissLimiter, Depends(get_public_miss_limiter)],
) -> HTMLResponse | StreamingResponse:
    """Return all business states as 200, except a separate abuse-limit response."""

    source = request.client.host if request.client is not None else "unknown"
    if await limiter.is_limited(source):
        return HTMLResponse(
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            "<title>产品资料</title></head><body><main><p>"
            "请求过于频繁, 请稍后再试。"
            "</p></main></body></html>",
            status_code=429,
            headers={**NO_STORE_HEADERS, "Retry-After": "600"},
        )

    document = await resolve_public_document(database, storage, public_token)
    if document.state == "missing":
        newly_limited = await limiter.register_miss(source)
        if newly_limited:
            await append_independent_event(
                database,
                AuditEvent(
                    action="public_token_probe",
                    result="failure",
                    actor_type="anonymous",
                    request_id=uuid4(),
                    detail={"miss_limit": limiter.limit},
                ),
            )
        return _status_page(STATUS_MESSAGES["missing"])
    if document.state == "disabled":
        return _status_page(STATUS_MESSAGES["disabled"])
    if document.state == "unuploaded":
        return _status_page(STATUS_MESSAGES["unuploaded"])
    if document.path is None or document.original_filename is None or document.size_bytes is None:
        raise RuntimeError("Available public document lacks file metadata")
    encoded_filename = quote(document.original_filename, safe="")
    headers = {
        **NO_STORE_HEADERS,
        "Content-Disposition": f"inline; filename*=UTF-8''{encoded_filename}",
        "Content-Length": str(document.size_bytes),
    }
    return StreamingResponse(
        _stream_file(document.path),
        status_code=200,
        media_type="application/pdf",
        headers=headers,
    )
