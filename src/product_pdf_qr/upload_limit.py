"""ASGI-level request limits applied before multipart file materialization."""

from __future__ import annotations

import logging
import re
from typing import cast
from uuid import UUID, uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from product_pdf_qr.config import get_settings
from product_pdf_qr.database import Database
from product_pdf_qr.domains.audit import AuditEvent, append_independent_event

logger = logging.getLogger(__name__)

MULTIPART_OVERHEAD_BYTES = 64 * 1024
PDF_UPLOAD_PATH = re.compile(
    r"^/api/products/(?P<product_id>[^/]+)/pdf$",
    re.ASCII,
)


class UploadRequestLimitMiddleware:
    """Bound PDF multipart requests before Starlette creates an UploadFile."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_pdf_bytes: int | None = None,
        multipart_overhead_bytes: int = MULTIPART_OVERHEAD_BYTES,
    ) -> None:
        self.app = app
        self.configured_max_pdf_bytes = max_pdf_bytes
        self.multipart_overhead_bytes = multipart_overhead_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._is_pdf_upload(scope):
            await self.app(scope, receive, send)
            return

        max_pdf_bytes = self.configured_max_pdf_bytes
        if max_pdf_bytes is None:
            max_pdf_bytes = get_settings().max_pdf_bytes
        max_request_bytes = max_pdf_bytes + self.multipart_overhead_bytes
        content_length = self._content_length(scope)
        if content_length is not None and content_length > max_request_bytes:
            request_id = uuid4()
            await self._audit_rejection(
                scope,
                request_id=request_id,
                detail={
                    "reason": "content_length_exceeded",
                    "stage": "pre_parser_size",
                    "declared_content_length": content_length,
                    "declared_request_body_verified": False,
                    "complete_pdf_byte_length_known": False,
                },
            )
            await self._reject(scope, receive, send, request_id=request_id)
            return

        received_bytes = 0
        too_large = False

        async def limited_receive() -> Message:
            nonlocal received_bytes, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > max_request_bytes:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def tracked_send(message: Message) -> None:
            if too_large:
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except Exception:
            if not too_large:
                raise
        if too_large:
            request_id = uuid4()
            await self._audit_rejection(
                scope,
                request_id=request_id,
                detail={
                    "reason": "chunked_stream_exceeded",
                    "stage": "pre_parser_size",
                    "received_bytes_before_abort": received_bytes,
                    "complete_pdf_byte_length_known": False,
                },
            )
            await self._reject(scope, receive, send, request_id=request_id)

    @staticmethod
    def _is_pdf_upload(scope: Scope) -> bool:
        return (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and PDF_UPLOAD_PATH.fullmatch(scope.get("path", "")) is not None
        )

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                parsed = int(value)
            except ValueError:
                return None
            return parsed if parsed >= 0 else None
        return None

    @staticmethod
    async def _audit_rejection(
        scope: Scope,
        *,
        request_id: UUID,
        detail: dict[str, object],
    ) -> None:
        match = PDF_UPLOAD_PATH.fullmatch(scope.get("path", ""))
        product_id: int | None = None
        if match is not None:
            try:
                candidate = int(match.group("product_id"))
            except ValueError:
                candidate = 0
            if candidate > 0:
                product_id = candidate
        try:
            database = cast(Database, scope["app"].state.database)
            written = await append_independent_event(
                database,
                AuditEvent(
                    action="pdf_upload_rejected",
                    result="failure",
                    actor_type="anonymous",
                    target_type="product",
                    target_id=product_id,
                    request_id=request_id,
                    detail=detail,
                ),
            )
            if not written:
                logger.error(
                    "Pre-parser upload rejection audit was not persisted",
                    extra={"request_id": str(request_id)},
                )
        except Exception:
            logger.exception(
                "Pre-parser upload rejection audit failed",
                extra={"request_id": str(request_id)},
            )

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        request_id: UUID,
    ) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "pdf_too_large",
                    "message": "PDF 文件不得超过 50 MB。",
                }
            },
            headers={
                "Connection": "close",
                "X-Request-ID": str(request_id),
            },
        )
        await response(scope, receive, send)
