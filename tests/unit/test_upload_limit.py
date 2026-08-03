"""HTTP-level evidence that oversized uploads stop before multipart parsing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, cast

import httpx
import pytest
from fastapi import FastAPI, File, Form, Request, UploadFile
from psycopg.types.json import Jsonb

from product_pdf_qr.upload_limit import UploadRequestLimitMiddleware
from tests.unit.test_business_services import (
    ScriptedConnection,
    ScriptedDatabase,
    as_database,
)


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


def limited_test_app() -> tuple[FastAPI, dict[str, bool], ScriptedConnection]:
    app = FastAPI()
    state = {"upload_handler_called": False}
    audit_connection = ScriptedConnection([None])
    app.state.database = as_database(ScriptedDatabase(audit_connection))
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

    return app, state, audit_connection


def assert_size_rejection_audit(
    audit_connection: ScriptedConnection,
    *,
    response: httpx.Response,
    expected_detail: dict[str, object],
) -> dict[str, object]:
    assert len(audit_connection.parameters) == 1
    parameters = audit_connection.parameters[0]
    assert isinstance(parameters, tuple)
    assert parameters[0] == "anonymous"
    assert parameters[1] is None
    assert parameters[2] == "pdf_upload_rejected"
    assert parameters[3] == "product"
    assert parameters[4] == 5
    assert parameters[6] == "failure"
    assert str(parameters[7]) == response.headers["x-request-id"]
    detail = parameters[8]
    assert isinstance(detail, Jsonb)
    assert detail.obj == expected_detail
    assert set(detail.obj).isdisjoint(
        {
            "actor_id",
            "file",
            "file_content",
            "form",
            "headers",
            "password",
            "request_body",
            "server_path",
            "token",
        }
    )
    return cast(dict[str, object], detail.obj)


@pytest.mark.anyio
async def test_content_length_rejection_reads_zero_request_bytes() -> None:
    complete_body = multipart_body(b"x" * 16)
    body = CountingBody([complete_body])
    app, state, audit_connection = limited_test_app()
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
    assert_size_rejection_audit(
        audit_connection,
        response=response,
        expected_detail={
            "reason": "content_length_exceeded",
            "stage": "pre_parser_size",
            "declared_request_body_bytes": len(complete_body),
            "declared_request_body_verified": False,
            "actual_file_size_known": False,
        },
    )


@pytest.mark.anyio
async def test_chunked_rejection_stops_before_consuming_complete_body() -> None:
    complete_body = multipart_body(b"x" * 64)
    chunks = [complete_body[index : index + 16] for index in range(0, len(complete_body), 16)]
    body = CountingBody(chunks)
    app, state, audit_connection = limited_test_app()
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
    received_bytes = sum(len(chunk) for chunk in body.chunks[: body.yielded_chunks])
    assert_size_rejection_audit(
        audit_connection,
        response=response,
        expected_detail={
            "reason": "chunked_stream_exceeded",
            "stage": "pre_parser_size",
            "received_request_body_bytes_before_abort": received_bytes,
            "actual_file_size_known": False,
        },
    )


@pytest.mark.anyio
async def test_upload_limit_does_not_apply_to_other_endpoints() -> None:
    body = CountingBody([b"x" * 32])
    app, _state, audit_connection = limited_test_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/unrelated", content=body)

    assert response.status_code == 200
    assert response.json() == {"received": 32}
    assert body.yielded_chunks == 1
    assert audit_connection.parameters == []


@pytest.mark.anyio
async def test_audit_failure_is_logged_without_blocking_early_413(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = CountingBody([b"must-not-be-read"])
    app, _state, _audit_connection = limited_test_app()
    app.state.database = as_database(
        ScriptedDatabase(
            ScriptedConnection([], execute_error=RuntimeError("synthetic audit failure"))
        )
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/products/5/pdf",
            content=body,
            headers={"Content-Length": "1000"},
        )

    assert response.status_code == 413
    assert body.yielded_chunks == 0
    assert "Pre-parser upload rejection audit was not persisted" in caplog.text
