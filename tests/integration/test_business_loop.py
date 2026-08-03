"""PostgreSQL-backed Phase 1-B API and concurrency acceptance tests."""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from pypdf import PdfWriter

from product_pdf_qr.config import get_settings
from product_pdf_qr.main import create_app, lifespan

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        pytest.fail(f"{name} is required for integration tests")
    return value


@contextmanager
def migration_environment(url: str) -> Iterator[None]:
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture
def clean_business_database() -> int:
    migration_url = required_environment("TEST_MIGRATION_DATABASE_URL")
    runtime_url = required_environment("TEST_DATABASE_URL")
    with migration_environment(migration_url):
        command.upgrade(Config("alembic.ini"), "head")
    migration_psycopg_url = migration_url.replace(
        "postgresql+psycopg://",
        "postgresql://",
    )
    with psycopg.connect(migration_psycopg_url, autocommit=True) as connection:
        connection.execute(
            """
            TRUNCATE TABLE
                audit_events,
                admin_sessions,
                pdf_versions,
                pdf_files,
                products,
                admins
            RESTART IDENTITY CASCADE
            """
        )
    with psycopg.connect(runtime_url) as connection:
        row = connection.execute(
            """
            INSERT INTO admins (
                username,
                password_hash,
                password_updated_at,
                created_at
            ) VALUES ('phase1b-synthetic-admin', 'authentication-not-in-phase1b', now(), now())
            RETURNING id
            """
        ).fetchone()
        connection.commit()
    assert row is not None
    return int(row[0])


def synthetic_pdf(width: int) -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=72)
    writer.write(output)
    return output.getvalue()


async def create_product(client: httpx.AsyncClient, code: str) -> dict[str, object]:
    response = await client.post("/api/products", json={"code": code})
    assert response.status_code == 201, response.text
    result = response.json()
    assert isinstance(result, dict)
    return result


async def upload(
    client: httpx.AsyncClient,
    product_id: int,
    actor_id: int,
    content: bytes,
    filename: str,
) -> httpx.Response:
    return await client.post(
        f"/api/products/{product_id}/pdf",
        data={"actor_id": str(actor_id)},
        files={"file": (filename, content, "application/pdf")},
    )


class ChunkedRequestBody(httpx.AsyncByteStream):
    """Track how many chunks the live ASGI stack consumes."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded_chunks = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yielded_chunks += 1
            yield chunk


def oversized_multipart_body() -> bytes:
    return (
        b"--integration-boundary\r\n"
        b'Content-Disposition: form-data; name="actor_id"\r\n\r\n'
        b"1\r\n"
        b"--integration-boundary\r\n"
        b'Content-Disposition: form-data; name="file"; filename="large.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n"
        + (b"x" * (80 * 1024))
        + b"\r\n--integration-boundary--\r\n"
    )


async def simulate_locked_restore(
    runtime_url: str,
    product_id: int,
    version_id: int,
) -> None:
    """Model the Phase 2 restore writer without exposing a management endpoint."""

    async with await psycopg.AsyncConnection.connect(runtime_url) as connection:
        async with connection.transaction():
            locked = await connection.execute(
                """
                SELECT id
                FROM products
                WHERE id = %s
                FOR UPDATE
                """,
                (product_id,),
            )
            assert await locked.fetchone() == (product_id,)
            target = await connection.execute(
                """
                SELECT id
                FROM pdf_versions
                WHERE product_id = %s AND id = %s
                """,
                (product_id, version_id),
            )
            assert await target.fetchone() == (version_id,)
            await connection.execute(
                """
                UPDATE products
                SET current_version_id = %s, updated_at = now()
                WHERE id = %s
                """,
                (version_id, product_id),
            )


@pytest.mark.e2e
async def test_minimum_business_loop_and_four_public_states(
    clean_business_database: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = clean_business_database
    runtime_url = required_environment("TEST_DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
    get_settings.cache_clear()
    app = create_app()
    first_pdf = synthetic_pdf(72)
    second_pdf = synthetic_pdf(144)

    try:
        async with lifespan(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                created = await create_product(client, " a001_1 ")
                product_id = cast(int, created["id"])
                token = str(created["public_token"])
                assert created["code"] == "A001_1"
                assert created["status"] == "active"
                assert created["current_version_id"] is None
                assert created["qrcode_status"] == "ready"

                qrcode = await client.get(str(created["qrcode_url"]))
                assert qrcode.status_code == 200
                assert qrcode.content.startswith(b"\x89PNG\r\n\x1a\n")
                assert qrcode.headers["content-disposition"] == 'attachment; filename="A001_1.png"'

                unuploaded = await client.get(f"/p/{token}")
                assert unuploaded.status_code == 200
                assert "资料暂未上传" in unuploaded.text
                assert unuploaded.headers["cache-control"] == "no-store"

                uploaded_v1 = await upload(
                    client,
                    product_id,
                    actor_id,
                    first_pdf,
                    "../../first.pdf",
                )
                assert uploaded_v1.status_code == 201, uploaded_v1.text
                assert uploaded_v1.json()["version_no"] == 1
                public_v1 = await client.get(f"/p/{token}")
                assert public_v1.status_code == 200
                assert public_v1.content == first_pdf
                assert public_v1.headers["content-type"] == "application/pdf"
                assert public_v1.headers["cache-control"] == "no-store"

                uploaded_v2 = await upload(
                    client,
                    product_id,
                    actor_id,
                    second_pdf,
                    "second.pdf",
                )
                assert uploaded_v2.status_code == 201
                assert uploaded_v2.json()["version_no"] == 2
                assert (await client.get(f"/p/{token}")).content == second_pdf

                uploaded_old_content = await upload(
                    client,
                    product_id,
                    actor_id,
                    first_pdf,
                    "first-again.pdf",
                )
                assert uploaded_old_content.status_code == 201
                assert uploaded_old_content.json()["version_no"] == 3
                assert (await client.get(f"/p/{token}")).content == first_pdf

                duplicate_current = await upload(
                    client,
                    product_id,
                    actor_id,
                    first_pdf,
                    "duplicate.pdf",
                )
                assert duplicate_current.status_code == 409
                assert duplicate_current.json()["error"]["message"] == "与当前文件相同"

                invalid_pdf = await upload(
                    client,
                    product_id,
                    actor_id,
                    b"not a pdf",
                    "fake.pdf",
                )
                assert invalid_pdf.status_code == 422

                missing = await client.get("/p/not-base32")
                malformed = await client.get("/p/invalid/token")
                assert missing.status_code == malformed.status_code == 200
                assert "资料链接无效" in missing.text
                assert "not-base32" not in missing.text

                with psycopg.connect(runtime_url) as connection:
                    connection.execute(
                        "UPDATE products SET status = 'disabled', updated_at = now() WHERE id = %s",
                        (product_id,),
                    )
                    connection.commit()
                disabled = await client.get(f"/p/{token}")
                assert disabled.status_code == 200
                assert "该产品资料已停用" in disabled.text
                assert "资料暂未上传" not in disabled.text
                assert "A001_1" not in disabled.text

        with psycopg.connect(runtime_url) as connection:
            version_count = connection.execute(
                "SELECT count(*) FROM pdf_versions WHERE product_id = %s",
                (product_id,),
            ).fetchone()
            file_count = connection.execute("SELECT count(*) FROM pdf_files").fetchone()
            actions = {
                str(row[0])
                for row in connection.execute(
                    "SELECT action FROM audit_events ORDER BY id"
                ).fetchall()
            }
        assert version_count == (3,)
        assert file_count == (2,)
        assert {"product_create", "pdf_upload", "pdf_upload_rejected"}.issubset(actions)
    finally:
        get_settings.cache_clear()


@pytest.mark.e2e
async def test_pre_parser_size_rejections_commit_independent_audits(
    clean_business_database: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_url = required_environment("TEST_DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("MAX_PDF_BYTES", "8")
    get_settings.cache_clear()
    app = create_app()
    declared_body = ChunkedRequestBody([b"must-not-be-read"])
    complete_chunked_body = oversized_multipart_body()
    chunks = [
        complete_chunked_body[index : index + (16 * 1024)]
        for index in range(0, len(complete_chunked_body), 16 * 1024)
    ]
    chunked_body = ChunkedRequestBody(chunks)

    try:
        async with lifespan(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                declared = await client.post(
                    "/api/products/5/pdf",
                    content=declared_body,
                    headers={
                        "Content-Length": "70000",
                        "Content-Type": "multipart/form-data; boundary=integration-boundary",
                    },
                )
                chunked = await client.post(
                    "/api/products/5/pdf",
                    content=chunked_body,
                    headers={"Content-Type": "multipart/form-data; boundary=integration-boundary"},
                )

        assert declared.status_code == chunked.status_code == 413
        assert declared_body.yielded_chunks == 0
        assert 0 < chunked_body.yielded_chunks < len(chunked_body.chunks)
        received_chunked_bytes = sum(
            len(chunk) for chunk in chunked_body.chunks[: chunked_body.yielded_chunks]
        )
        with psycopg.connect(runtime_url) as connection:
            rows = connection.execute(
                """
                SELECT actor_type, actor_id, target_type, target_id, request_id, detail
                FROM audit_events
                WHERE action = 'pdf_upload_rejected'
                ORDER BY id
                """
            ).fetchall()
        assert len(rows) == 2
        assert {str(row[0]) for row in rows} == {"anonymous"}
        assert {row[1] for row in rows} == {None}
        assert {str(row[2]) for row in rows} == {"product"}
        assert {int(row[3]) for row in rows} == {5}
        assert str(rows[0][4]) == declared.headers["x-request-id"]
        assert str(rows[1][4]) == chunked.headers["x-request-id"]
        assert rows[0][5] == {
            "reason": "content_length_exceeded",
            "stage": "pre_parser_size",
            "declared_content_length": 70000,
            "declared_request_body_verified": False,
            "complete_pdf_byte_length_known": False,
        }
        assert rows[1][5] == {
            "reason": "chunked_stream_exceeded",
            "stage": "pre_parser_size",
            "received_bytes_before_abort": received_chunked_bytes,
            "complete_pdf_byte_length_known": False,
        }
    finally:
        get_settings.cache_clear()


@pytest.mark.e2e
async def test_concurrent_identical_uploads_create_only_one_version(
    clean_business_database: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = clean_business_database
    runtime_url = required_environment("TEST_DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "10")
    get_settings.cache_clear()
    app = create_app()
    content = synthetic_pdf(72)

    try:
        async with lifespan(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                create_responses = await asyncio.gather(
                    *[
                        client.post("/api/products", json={"code": "CONCURRENT"})
                        for _index in range(6)
                    ]
                )
                assert [response.status_code for response in create_responses].count(201) == 1
                assert [response.status_code for response in create_responses].count(409) == 5
                created_response = next(
                    response for response in create_responses if response.status_code == 201
                )
                created = created_response.json()
                assert isinstance(created, dict)
                product_id = cast(int, created["id"])
                responses = await asyncio.gather(
                    *[
                        upload(
                            client,
                            product_id,
                            actor_id,
                            content,
                            f"same-{index}.pdf",
                        )
                        for index in range(8)
                    ]
                )

                different = await create_product(client, "DIFFERENT-CONTENT")
                different_product_id = cast(int, different["id"])
                different_responses = await asyncio.gather(
                    upload(
                        client,
                        different_product_id,
                        actor_id,
                        synthetic_pdf(100),
                        "different-a.pdf",
                    ),
                    upload(
                        client,
                        different_product_id,
                        actor_id,
                        synthetic_pdf(200),
                        "different-b.pdf",
                    ),
                )
                assert [response.status_code for response in different_responses] == [201, 201]

                parallel_a = await create_product(client, "PARALLEL-A")
                parallel_b = await create_product(client, "PARALLEL-B")
                parallel_responses = await asyncio.gather(
                    upload(
                        client,
                        cast(int, parallel_a["id"]),
                        actor_id,
                        synthetic_pdf(300),
                        "parallel-a.pdf",
                    ),
                    upload(
                        client,
                        cast(int, parallel_b["id"]),
                        actor_id,
                        synthetic_pdf(400),
                        "parallel-b.pdf",
                    ),
                )
                assert [response.status_code for response in parallel_responses] == [201, 201]

                restore_race = await create_product(client, "RESTORE-RACE")
                restore_product_id = cast(int, restore_race["id"])
                restore_v1 = await upload(
                    client,
                    restore_product_id,
                    actor_id,
                    synthetic_pdf(500),
                    "restore-v1.pdf",
                )
                restore_v2 = await upload(
                    client,
                    restore_product_id,
                    actor_id,
                    synthetic_pdf(600),
                    "restore-v2.pdf",
                )
                assert restore_v1.status_code == restore_v2.status_code == 201
                restore_v1_id = cast(int, restore_v1.json()["version_id"])
                restore_v3, _ = await asyncio.gather(
                    upload(
                        client,
                        restore_product_id,
                        actor_id,
                        synthetic_pdf(700),
                        "restore-v3.pdf",
                    ),
                    simulate_locked_restore(
                        runtime_url,
                        restore_product_id,
                        restore_v1_id,
                    ),
                )
                assert restore_v3.status_code == 201
                restore_v3_id = cast(int, restore_v3.json()["version_id"])

        assert [response.status_code for response in responses].count(201) == 1
        assert [response.status_code for response in responses].count(409) == 7
        with psycopg.connect(runtime_url) as connection:
            assert connection.execute(
                "SELECT count(*) FROM pdf_versions WHERE product_id = %s",
                (product_id,),
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT count(*) FROM pdf_versions WHERE product_id = %s",
                (different_product_id,),
            ).fetchone() == (2,)
            current = connection.execute(
                "SELECT current_version_id FROM products WHERE id = %s",
                (product_id,),
            ).fetchone()
            restore_current = connection.execute(
                """
                SELECT p.current_version_id, v.product_id
                FROM products AS p
                JOIN pdf_versions AS v ON v.id = p.current_version_id
                WHERE p.id = %s
                """,
                (restore_product_id,),
            ).fetchone()
            restore_version_count = connection.execute(
                "SELECT count(*) FROM pdf_versions WHERE product_id = %s",
                (restore_product_id,),
            ).fetchone()
        assert current is not None and current[0] is not None
        assert restore_version_count == (3,)
        assert restore_current is not None
        assert restore_current[0] in {restore_v1_id, restore_v3_id}
        assert restore_current[1] == restore_product_id
    finally:
        get_settings.cache_clear()
