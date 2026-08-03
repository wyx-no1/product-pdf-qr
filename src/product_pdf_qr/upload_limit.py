"""ASGI-level request limits applied before multipart file materialization."""

from __future__ import annotations

import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from product_pdf_qr.config import get_settings

MULTIPART_OVERHEAD_BYTES = 64 * 1024
PDF_UPLOAD_PATH = re.compile(r"^/api/products/[^/]+/pdf$", re.ASCII)


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
            await self._reject(scope, receive, send)
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
            await self._reject(scope, receive, send)

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
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "pdf_too_large",
                    "message": "PDF 文件不得超过 50 MB。",
                }
            },
            headers={"Connection": "close"},
        )
        await response(scope, receive, send)
