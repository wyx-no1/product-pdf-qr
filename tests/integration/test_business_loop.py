"""PostgreSQL-backed Phase 1-B API and concurrency acceptance tests."""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from pypdf import PdfWriter

from product_pdf_qr.config import Settings, get_settings
from product_pdf_qr.database import Database
from product_pdf_qr.domains.auth import (
    SESSION_COOKIE_NAME,
    PasswordManager,
    create_admin,
    hash_session_token,
)
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
def clean_business_database() -> AuthContext:
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

    async def provision_admin() -> int:
        database = Database(Settings.model_validate({"database_url": runtime_url}))
        await database.open()
        try:
            return await create_admin(
                database,
                PasswordManager(),
                raw_username="phase1b-synthetic-admin",
                password="FixtureTemporaryPassword-123",
            )
        finally:
            await database.close()

    admin_id = asyncio.run(provision_admin())
    with psycopg.connect(runtime_url) as connection:
        connection.execute(
            "UPDATE admins SET must_change_password = false WHERE id = %s",
            (admin_id,),
        )
        session_token = "integration-session-token"
        connection.execute(
            """
            INSERT INTO admin_sessions (
                id,
                admin_id,
                token_hash,
                issued_at,
                expires_at
            ) VALUES (%s, %s, %s, now(), now() + interval '1 hour')
            """,
            (uuid4(), admin_id, hash_session_token(session_token)),
        )
        connection.commit()
    return AuthContext(admin_id=admin_id, session_token=session_token)


@dataclass(frozen=True, slots=True)
class AuthContext:
    admin_id: int
    session_token: str


def synthetic_pdf(width: int) -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=72)
    writer.write(output)
    return output.getvalue()


async def create_product(
    client: httpx.AsyncClient,
    code: str,
    name: str | None = None,
) -> dict[str, object]:
    product_name = name or f"{code.strip()} 产品"
    response = await client.post(
        "/api/products",
        json={"code": code, "name": product_name},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert isinstance(result, dict)
    assert result["name"] == product_name.strip()
    return result


async def upload(
    client: httpx.AsyncClient,
    product_id: int,
    content: bytes,
    filename: str,
) -> httpx.Response:
    return await client.post(
        f"/api/products/{product_id}/pdf",
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
async def test_login_force_change_session_revocation_and_logout(
    clean_business_database: AuthContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_url = required_environment("TEST_DATABASE_URL")
    temporary_password = "TemporaryPassword-123"
    permanent_password = "PermanentPassword-456"
    password_hash = PasswordManager().hash(temporary_password)
    with psycopg.connect(runtime_url) as connection:
        connection.execute("DELETE FROM admin_sessions")
        connection.execute(
            """
            UPDATE admins
            SET
                password_hash = %s,
                must_change_password = true,
                password_updated_at = now()
            WHERE id = %s
            """,
            (password_hash, clean_business_database.admin_id),
        )
        connection.commit()

    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("LOGIN_FAILURE_LIMIT", "2")
    monkeypatch.setenv("LOGIN_BACKOFF_BASE_SECONDS", "30")
    monkeypatch.setenv("LOGIN_BACKOFF_MAX_SECONDS", "30")
    get_settings.cache_clear()
    app = create_app()

    try:
        async with lifespan(app):
            transport = httpx.ASGITransport(app=app)
            async with (
                httpx.AsyncClient(transport=transport, base_url="http://test") as first,
                httpx.AsyncClient(transport=transport, base_url="http://test") as second,
            ):
                unauthenticated = await first.get("/admin")
                assert unauthenticated.status_code == 303
                assert unauthenticated.headers["location"].startswith("/admin/login")

                wrong = await first.post(
                    "/admin/login",
                    data={
                        "username": "phase1b-synthetic-admin",
                        "password": "WrongPassword-123",
                        "next": "/admin",
                    },
                )
                assert wrong.status_code == 401
                assert "用户名或密码错误" in wrong.text

                first_login = await first.post(
                    "/admin/login",
                    data={
                        "username": "phase1b-synthetic-admin",
                        "password": temporary_password,
                        "next": "/admin",
                    },
                )
                second_login = await second.post(
                    "/admin/login",
                    data={
                        "username": "phase1b-synthetic-admin",
                        "password": temporary_password,
                        "next": "/admin",
                    },
                )
                assert first_login.status_code == second_login.status_code == 303
                assert first_login.headers["location"] == "/admin/change-password"
                cookie_header = first_login.headers["set-cookie"]
                assert "HttpOnly" in cookie_header
                assert "SameSite=lax" in cookie_header
                assert "Secure" not in cookie_header

                bypass = await first.get("/api/products")
                assert bypass.status_code == 303
                assert bypass.headers["location"] == "/admin/change-password"
                assert "public_token" not in bypass.text

                unchanged = await first.post(
                    "/admin/change-password",
                    data={
                        "current_password": temporary_password,
                        "new_password": temporary_password,
                        "confirm_password": temporary_password,
                    },
                )
                assert unchanged.status_code == 422
                assert "新密码必须与当前密码不同" in unchanged.text

                changed = await first.post(
                    "/admin/change-password",
                    data={
                        "current_password": temporary_password,
                        "new_password": permanent_password,
                        "confirm_password": permanent_password,
                    },
                )
                assert changed.status_code == 303
                assert changed.headers["location"] == "/admin"
                assert (await first.get("/admin")).status_code == 200

                revoked_other = await second.get("/admin")
                assert revoked_other.status_code == 303
                assert revoked_other.headers["location"].startswith("/admin/login")

                logout = await first.post("/admin/logout")
                assert logout.status_code == 303
                assert (await first.get("/admin")).status_code == 303

                for _index in range(2):
                    limited_failure = await first.post(
                        "/admin/login",
                        data={
                            "username": "unknown-admin",
                            "password": "WrongPassword-123",
                            "next": "/admin",
                        },
                    )
                    assert limited_failure.status_code == 401
                blocked = await first.post(
                    "/admin/login",
                    data={
                        "username": "unknown-admin",
                        "password": "WrongPassword-123",
                        "next": "/admin",
                    },
                )
                assert blocked.status_code == 429
                assert int(blocked.headers["retry-after"]) >= 1

        with psycopg.connect(runtime_url) as connection:
            session_rows = connection.execute(
                "SELECT token_hash, revoked_at FROM admin_sessions ORDER BY issued_at"
            ).fetchall()
            admin_state = connection.execute(
                """
                SELECT must_change_password, last_login_at
                FROM admins
                WHERE id = %s
                """,
                (clean_business_database.admin_id,),
            ).fetchone()
            password_change_audit = connection.execute(
                "SELECT count(*) FROM audit_events WHERE action = 'password_change'"
            ).fetchone()
        assert session_rows
        assert all(len(str(row[0])) == 64 for row in session_rows)
        assert all(row[1] is not None for row in session_rows)
        assert admin_state is not None
        assert admin_state[0] is False
        assert admin_state[1] is not None
        assert password_change_audit == (1,)
    finally:
        get_settings.cache_clear()


@pytest.mark.e2e
async def test_minimum_business_loop_and_four_public_states(
    clean_business_database: AuthContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = clean_business_database
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
            async with (
                httpx.AsyncClient(
                    transport=transport,
                    base_url="http://test",
                    cookies={SESSION_COOKIE_NAME: auth.session_token},
                ) as client,
                httpx.AsyncClient(transport=transport, base_url="http://test") as public_client,
            ):
                created = await create_product(client, " a001_1 ", " 一号测试产品 ")
                product_id = cast(int, created["id"])
                token = str(created["public_token"])
                assert created["code"] == "A001_1"
                assert created["name"] == "一号测试产品"
                assert created["status"] == "active"
                assert created["current_version_id"] is None
                assert created["qrcode_status"] == "ready"

                product_list = await client.get("/api/products?limit=10&offset=0")
                assert product_list.status_code == 200
                assert product_list.json()[0]["name"] == "一号测试产品"
                assert product_list.json()[0]["pdf_status"] == "not_uploaded"

                detail = await client.get(f"/api/products/{product_id}")
                assert detail.status_code == 200
                detail_body = detail.json()
                assert detail_body["name"] == "一号测试产品"
                assert detail_body["pdf_status"] == "not_uploaded"
                assert detail_body["qrcode_status"] == "ready"
                assert detail_body["public_token"] == token

                with psycopg.connect(runtime_url) as connection:
                    historical_row = connection.execute(
                        """
                        INSERT INTO products (
                            code,
                            public_token,
                            created_at,
                            updated_at
                        ) VALUES ('HISTORICAL-NULL', 'HHHHHHHHHHHHHHHHHHHHHHHHHH', now(), now())
                        RETURNING id
                        """
                    ).fetchone()
                    connection.commit()
                assert historical_row is not None
                historical_id = int(historical_row[0])
                historical_detail = await client.get(f"/api/products/{historical_id}")
                assert historical_detail.status_code == 200
                assert historical_detail.json()["name"] is None
                product_list_with_history = await client.get("/api/products")
                assert any(
                    product["id"] == historical_id and product["name"] is None
                    for product in product_list_with_history.json()
                )
                missing_detail = await client.get("/api/products/999999")
                assert missing_detail.status_code == 404
                assert missing_detail.json()["error"]["code"] == "product_not_found"

                qrcode = await client.get(str(created["qrcode_url"]))
                assert qrcode.status_code == 200
                assert qrcode.content.startswith(b"\x89PNG\r\n\x1a\n")
                assert qrcode.headers["content-disposition"] == 'attachment; filename="A001_1.png"'

                unuploaded = await public_client.get(f"/p/{token}")
                assert unuploaded.status_code == 200
                assert "资料暂未上传" in unuploaded.text
                assert unuploaded.headers["cache-control"] == "no-store"

                uploaded_v1 = await upload(
                    client,
                    product_id,
                    first_pdf,
                    "../../first.pdf",
                )
                assert uploaded_v1.status_code == 201, uploaded_v1.text
                assert uploaded_v1.json()["version_no"] == 1
                refreshed_detail = await client.get(f"/api/products/{product_id}")
                assert refreshed_detail.json()["pdf_status"] == "uploaded"
                assert (
                    refreshed_detail.json()["current_version_id"]
                    == uploaded_v1.json()["version_id"]
                )
                public_v1 = await public_client.get(f"/p/{token}")
                assert public_v1.status_code == 200
                assert public_v1.content == first_pdf
                assert public_v1.headers["content-type"] == "application/pdf"
                assert public_v1.headers["cache-control"] == "no-store"

                uploaded_v2 = await upload(
                    client,
                    product_id,
                    second_pdf,
                    "second.pdf",
                )
                assert uploaded_v2.status_code == 201
                assert uploaded_v2.json()["version_no"] == 2
                assert (await public_client.get(f"/p/{token}")).content == second_pdf

                uploaded_old_content = await upload(
                    client,
                    product_id,
                    first_pdf,
                    "first-again.pdf",
                )
                assert uploaded_old_content.status_code == 201
                assert uploaded_old_content.json()["version_no"] == 3
                assert (await public_client.get(f"/p/{token}")).content == first_pdf

                duplicate_current = await upload(
                    client,
                    product_id,
                    first_pdf,
                    "duplicate.pdf",
                )
                assert duplicate_current.status_code == 409
                assert duplicate_current.json()["error"]["message"] == "与当前文件相同"

                invalid_pdf = await upload(
                    client,
                    product_id,
                    b"not a pdf",
                    "fake.pdf",
                )
                assert invalid_pdf.status_code == 422

                missing = await public_client.get("/p/not-base32")
                malformed = await public_client.get("/p/invalid/token")
                assert missing.status_code == malformed.status_code == 200
                assert "资料链接无效" in missing.text
                assert "not-base32" not in missing.text

                with psycopg.connect(runtime_url) as connection:
                    connection.execute(
                        "UPDATE products SET status = 'disabled', updated_at = now() WHERE id = %s",
                        (product_id,),
                    )
                    connection.commit()
                disabled = await public_client.get(f"/p/{token}")
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
            upload_actors = connection.execute(
                "SELECT DISTINCT uploaded_by FROM pdf_versions WHERE product_id = %s",
                (product_id,),
            ).fetchall()
            actions = {
                str(row[0])
                for row in connection.execute(
                    "SELECT action FROM audit_events ORDER BY id"
                ).fetchall()
            }
        assert version_count == (3,)
        assert file_count == (2,)
        assert upload_actors == [(auth.admin_id,)]
        assert {"product_create", "pdf_upload", "pdf_upload_rejected"}.issubset(actions)
    finally:
        get_settings.cache_clear()


@pytest.mark.e2e
async def test_pre_parser_size_rejections_commit_independent_audits(
    clean_business_database: AuthContext,
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
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                cookies={SESSION_COOKIE_NAME: clean_business_database.session_token},
            ) as client:
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
    clean_business_database: AuthContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = clean_business_database
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
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                cookies={SESSION_COOKIE_NAME: auth.session_token},
            ) as client:
                create_responses = await asyncio.gather(
                    *[
                        client.post(
                            "/api/products",
                            json={"code": "CONCURRENT", "name": "并发产品"},
                        )
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
                        synthetic_pdf(100),
                        "different-a.pdf",
                    ),
                    upload(
                        client,
                        different_product_id,
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
                        synthetic_pdf(300),
                        "parallel-a.pdf",
                    ),
                    upload(
                        client,
                        cast(int, parallel_b["id"]),
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
                    synthetic_pdf(500),
                    "restore-v1.pdf",
                )
                restore_v2 = await upload(
                    client,
                    restore_product_id,
                    synthetic_pdf(600),
                    "restore-v2.pdf",
                )
                assert restore_v1.status_code == restore_v2.status_code == 201
                restore_v1_id = cast(int, restore_v1.json()["version_id"])
                restore_v3, _ = await asyncio.gather(
                    upload(
                        client,
                        restore_product_id,
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
