"""HTTP-level evidence that oversized uploads stop before multipart parsing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import httpx
import pytest
from fastapi import FastAPI, File, Form, Request, UploadFile

from product_pdf_qr.upload_limit import UploadRequestLimitMiddleware


class CountingBody(httpx.AsyncByteStream):
    """A request stream that records exactly how much the ASGI app consumed."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded_chunks = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yielded_chunks += 1
            yield chunk


def multipart_body(payload: bytes) -> bytes:
    return (
        b"--test-boundary\r\n"
        b'Content-Disposition: form-data; name="actor_id"\r\n\r\n'
        b"9\r\n"
        b"--test-boundary\r\n"
        b'Content-Disposition: form-data; name="file"; filename="test.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n" + payload + b"\r\n--test-boundary--\r\n"
    )


def limited_test_app() -> tuple[FastAPI, dict[str, bool]]:
    app = FastAPI()
    state = {"upload_handler_called": False}
    app.add_middleware(
        UploadRequestLimitMiddleware,
        max_pdf_bytes=8,
        multipart_overhead_bytes=64,
    )

    @app.post("/api/products/{product_id}/pdf")
    async def consume_upload(
        product_id: int,
        actor_id: Annotated[int, Form()],
        file: Annotated[UploadFile, File()],
    ) -> dict[str, int]:
        state["upload_handler_called"] = True
        return {
            "product_id": product_id,
            "actor_id": actor_id,
            "received": len(await file.read()),
        }

    @app.post("/unrelated")
    async def consume_unrelated(request: Request) -> dict[str, int]:
        return {"received": len(await request.body())}

    return app, state


@pytest.mark.anyio
async def test_content_length_rejection_reads_zero_request_bytes() -> None:
    complete_body = multipart_body(b"x" * 16)
    body = CountingBody([complete_body])
    app, state = limited_test_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/products/5/pdf",
            content=body,
            headers={
                "Content-Length": str(len(complete_body)),
                "Content-Type": "multipart/form-data; boundary=test-boundary",
            },
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "pdf_too_large"
    assert body.yielded_chunks == 0
    assert not state["upload_handler_called"]


@pytest.mark.anyio
async def test_chunked_rejection_stops_before_consuming_complete_body() -> None:
    complete_body = multipart_body(b"x" * 64)
    chunks = [complete_body[index : index + 16] for index in range(0, len(complete_body), 16)]
    body = CountingBody(chunks)
    app, state = limited_test_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/products/5/pdf",
            content=body,
            headers={"Content-Type": "multipart/form-data; boundary=test-boundary"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "pdf_too_large"
    assert body.yielded_chunks > 0
    assert body.yielded_chunks < len(body.chunks)
    assert not state["upload_handler_called"]


@pytest.mark.anyio
async def test_upload_limit_does_not_apply_to_other_endpoints() -> None:
    body = CountingBody([b"x" * 32])
    app, _state = limited_test_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/unrelated", content=body)

    assert response.status_code == 200
    assert response.json() == {"received": 32}
    assert body.yielded_chunks == 1
