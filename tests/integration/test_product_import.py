"""PostgreSQL-backed G-03 Excel import acceptance, concurrency, and audit tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import os
import zipfile
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config

from product_pdf_qr.config import Settings, get_settings
from product_pdf_qr.database import Connection, Database
from product_pdf_qr.domains.auth import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    PasswordManager,
    create_admin,
    csrf_token_for_session,
    hash_session_token,
)
from product_pdf_qr.domains.importer.parser import XlsxRejected
from product_pdf_qr.domains.product import (
    Product,
    create_product,
    create_product_in_transaction,
)
from product_pdf_qr.main import create_app, lifespan
from tests.xlsx_helpers import Worksheet, build_sparse_wide_xlsx, build_xlsx

pytestmark = [pytest.mark.integration, pytest.mark.anyio]
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(frozen=True, slots=True)
class ImportAuth:
    admin_id: int
    session_token: str
    csrf_token: str


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
def clean_import_database() -> ImportAuth:
    migration_url = required_environment("TEST_MIGRATION_DATABASE_URL")
    runtime_url = required_environment("TEST_DATABASE_URL")
    with migration_environment(migration_url):
        command.upgrade(Config("alembic.ini"), "head")
    migration_psycopg_url = migration_url.replace("postgresql+psycopg://", "postgresql://")
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
                raw_username="excel-import-admin",
                password="FixtureTemporaryPassword-123",
            )
        finally:
            await database.close()

    admin_id = asyncio.run(provision_admin())
    session_token = "excel-import-session"
    with psycopg.connect(runtime_url) as connection:
        connection.execute(
            "UPDATE admins SET must_change_password = false WHERE id = %s",
            (admin_id,),
        )
        connection.execute(
            """
            INSERT INTO admin_sessions (
                id, admin_id, token_hash, issued_at, expires_at
            ) VALUES (%s, %s, %s, now(), now() + interval '1 hour')
            """,
            (uuid4(), admin_id, hash_session_token(session_token)),
        )
        connection.commit()
    return ImportAuth(
        admin_id=admin_id,
        session_token=session_token,
        csrf_token=csrf_token_for_session(session_token),
    )


@asynccontextmanager
async def running_clients(
    auth: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **environment: str,
) -> AsyncIterator[tuple[httpx.AsyncClient, httpx.AsyncClient]]:
    monkeypatch.setenv("DATABASE_URL", required_environment("TEST_DATABASE_URL"))
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    for name, value in environment.items():
        monkeypatch.setenv(name.upper(), value)
    get_settings.cache_clear()
    app = create_app()
    try:
        async with lifespan(app):
            transport = httpx.ASGITransport(app=app)
            async with (
                httpx.AsyncClient(
                    transport=transport,
                    base_url="http://test",
                    cookies={SESSION_COOKIE_NAME: auth.session_token},
                    headers={CSRF_HEADER_NAME: auth.csrf_token},
                ) as client,
                httpx.AsyncClient(transport=transport, base_url="http://test") as anonymous,
            ):
                yield client, anonymous
    finally:
        get_settings.cache_clear()


async def post_import(client: httpx.AsyncClient, content: bytes) -> httpx.Response:
    return await client.post(
        "/api/product-imports",
        files={"file": ("products.xlsx", content, XLSX_MIME)},
    )


async def post_import_page(
    client: httpx.AsyncClient,
    auth: ImportAuth,
    content: bytes,
) -> httpx.Response:
    return await client.post(
        "/admin/imports",
        data={"csrf_token": auth.csrf_token},
        files={"file": ("products.xlsx", content, XLSX_MIME)},
    )


def product_rows() -> list[tuple[object, ...]]:
    with psycopg.connect(required_environment("TEST_DATABASE_URL")) as connection:
        return connection.execute(
            """
            SELECT code, name, public_token, status, current_version_id, created_at, updated_at
            FROM products
            ORDER BY code
            """
        ).fetchall()


def audit_rows(result: str | None = None) -> list[tuple[object, ...]]:
    parameters: tuple[object, ...] = ()
    result_clause = ""
    if result is not None:
        result_clause = "AND result = %s"
        parameters = (result,)
    with psycopg.connect(required_environment("TEST_DATABASE_URL")) as connection:
        return connection.execute(
            f"""
            SELECT action, result, actor_type, actor_id, detail
            FROM audit_events
            WHERE action = 'product_import' {result_clause}
            ORDER BY id
            """,
            parameters,
        ).fetchall()


def sheet(rows: Sequence[tuple[int, tuple[str, ...]]]) -> bytes:
    return build_xlsx([rows])


@pytest.mark.api
@pytest.mark.e2e
async def test_tc01_100_mixed_rows_default_names_and_no_qrcode(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[tuple[int, tuple[str, ...]]] = [(1, ("编码", "名称"))]
    for index in range(100):
        raw_code = f" p{index:03} " if index % 2 else f"P{index:03}".lower()
        name = "" if index < 20 else f"产品 {index}"
        rows.append((index + 2, (raw_code, name)))
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        response = await post_import(client, sheet(rows))
    assert response.status_code == 200
    assert response.json()["success_count"] == 100
    assert response.json()["duplicate_count"] == response.json()["format_error_count"] == 0
    persisted = product_rows()
    assert len(persisted) == 100
    assert all(str(row[0]) == f"P{index:03}" for index, row in enumerate(persisted))
    assert all(str(persisted[index][1]) == f"P{index:03}" for index in range(20))
    assert all(len(str(row[2])) == 26 for row in persisted)
    assert all(row[4] is None for row in persisted)
    assert not list(tmp_path.rglob("*.png"))


@pytest.mark.api
async def test_tc02_database_duplicates_skip_without_overwrite(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        created = await client.post("/api/products", json={"code": "A001", "name": "原始名称"})
        assert created.status_code == 201
        before = next(row for row in product_rows() if row[0] == "A001")
        rows = [(1, ("编码", "名称"))]
        rows.extend(
            [
                (2, ("a001", "覆盖 1")),
                (3, (" A001 ", "覆盖 2")),
                (4, ("A001", "覆盖 3")),
            ]
        )
        rows.extend((index + 5, (f"N{index:03}", f"新增 {index}")) for index in range(97))
        response = await post_import(client, sheet(rows))
    assert (
        response.json()["success_count"],
        response.json()["duplicate_count"],
        response.json()["format_error_count"],
    ) == (97, 3, 0)
    after = next(row for row in product_rows() if row[0] == "A001")
    assert after == before
    assert len(product_rows()) == 98


@pytest.mark.api
async def test_tc03_file_and_database_duplicates_share_one_count(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        assert (
            await client.post("/api/products", json={"code": "DB001", "name": "数据库原名"})
        ).status_code == 201
        response = await post_import(
            client,
            sheet(
                [
                    (1, ("编码", "名称")),
                    (2, ("a001", "首名")),
                    (3, (" A001 ", "次名")),
                    (4, ("A001", "末名")),
                    (5, ("db001", "覆盖名")),
                    (6, ("NEW001", "新增")),
                ]
            ),
        )
    assert response.json()["success_count"] == 2
    assert response.json()["duplicate_count"] == 3
    names = {str(row[0]): str(row[1]) for row in product_rows()}
    assert names == {"A001": "首名", "DB001": "数据库原名", "NEW001": "新增"}


@pytest.mark.api
@pytest.mark.parametrize(
    ("case_id", "row_number", "values", "reason"),
    [
        ("TC-05", 3, ("A" * 65, "过长"), "超过 64"),
        ("TC-06", 4, ("A中001", "中文"), "非法字符"),
        ("TC-07", 3, ("A 001", "空格"), "内部空格"),
        ("TC-08", 3, ("A#001", "特殊"), "非法字符"),
        ("TC-09", 3, ("LONGNAME", "名" * 121), "超过 120"),
        ("TC-10", 2, ("", "仅名称"), "编码为空"),
    ],
)
async def test_tc05_to_tc10_format_failures_are_atomic_and_audited(
    case_id: str,
    row_number: int,
    values: tuple[str, str],
    reason: str,
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del case_id
    rows = [(1, ("编码", "名称")), (2, ("VALID", "合法"))]
    if row_number == 2:
        rows[1] = (2, values)
        rows.append((3, ("VALID", "合法")))
    else:
        if row_number == 4:
            rows.append((3, (" ", " ")))
        rows.append((row_number, values))
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        response = await post_import(client, sheet(rows))
    body = response.json()
    assert response.status_code == 422
    assert (body["success_count"], body["duplicate_count"], body["format_error_count"]) == (
        0,
        0,
        1,
    )
    assert body["errors"][0]["row"] == row_number
    assert reason in body["errors"][0]["reason"]
    assert product_rows() == []
    failure_audits = audit_rows("failure")
    assert len(failure_audits) == 1
    assert cast(dict[str, object], failure_audits[0][4])["format_error_count"] == 1


@pytest.mark.api
@pytest.mark.parametrize(
    ("headers", "expected_code"),
    [
        ((" SKU ", "自定义 名称", "备注#"), "missing_code_column"),
        (("编码", "产品编码"), "ambiguous_code_column"),
        (("编码", " 编码 "), "ambiguous_code_column"),
    ],
)
async def test_tc13_tc14_tc15_header_failures_are_atomic_and_audited(
    headers: tuple[str, ...],
    expected_code: str,
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        response = await post_import(client, sheet([(1, headers), (2, ("A", "B", "C"))]))
    assert response.status_code == 422
    assert response.json()["error_code"] == expected_code
    assert response.json()["format_error_count"] == 1
    if expected_code == "missing_code_column":
        assert all(header in response.json()["errors"][0]["reason"] for header in headers)
    assert product_rows() == []
    assert len(audit_rows("failure")) == 1


@pytest.mark.api
@pytest.mark.e2e
async def test_tc16_ten_mb_plus_one_rejected_before_parser_and_visible(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_calls = 0

    def forbidden_inspection(*args: object, **kwargs: object) -> object:
        nonlocal parser_calls
        parser_calls += 1
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.inspect_xlsx_container",
        forbidden_inspection,
    )
    payload = b"x" * (10 * 1024 * 1024 + 1)
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        response = await post_import_page(client, clean_import_database, payload)
    assert response.status_code == 413
    assert "文件超过 10 MB 上限" in response.text
    assert parser_calls == 0
    assert product_rows() == []
    detail = cast(dict[str, object], audit_rows("failure")[-1][4])
    assert detail["reason"] in {"upload_size_exceeded", "content_length_exceeded"}
    assert 10 * 1024 * 1024 in {
        detail.get("max_upload_bytes"),
        detail.get("max_payload_bytes"),
    }


@pytest.mark.api
async def test_tc17_5001_rows_rejected_with_actual_value(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [(1, ("编码",))]
    rows.extend((index + 2, (f"R{index:04}",)) for index in range(5_001))
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        response = await post_import(client, sheet(rows))
    assert response.status_code == 413
    assert product_rows() == []
    detail = cast(dict[str, object], audit_rows("failure")[-1][4])
    assert detail["actual_rows"] == 5_001
    assert detail["max_rows"] == 5_000


@pytest.mark.api
async def test_sparse_wide_row_limit_is_rejected_during_parse_and_audited(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attack = build_sparse_wide_xlsx(50_000)
    assert len(attack) < 10 * 1024 * 1024
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        response = await post_import(client, attack)
    assert response.status_code == 413
    assert response.json()["error_code"] == "xlsx_row_limit_exceeded"
    assert product_rows() == []
    failure_audits = audit_rows("failure")
    assert len(failure_audits) == 1
    detail = cast(dict[str, object], failure_audits[0][4])
    assert detail["reason"] == "row_limit_exceeded"
    assert detail["actual_rows"] == 5_001
    assert detail["max_rows"] == 5_000
    assert detail["success_count"] == 0
    assert detail["duplicate_count"] == 0
    assert detail["format_error_count"] == 0


@pytest.mark.api
async def test_tc18_fake_xlsx_signature_rejected_before_parser(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_calls = 0

    async def forbidden_parser(*args: object, **kwargs: object) -> object:
        nonlocal parser_calls
        parser_calls += 1
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.parse_xlsx_with_timeout",
        forbidden_parser,
    )
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        response = await post_import(client, b"plain text")
    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_xlsx_signature"
    assert parser_calls == 0
    assert product_rows() == []


@pytest.mark.api
@pytest.mark.parametrize("case_id", ["decompressed", "ratio"])
async def test_tc19_tc20_zip_bounds_reject_before_xml_parser(
    case_id: str,
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_calls = 0

    async def forbidden_parser(*args: object, **kwargs: object) -> object:
        nonlocal parser_calls
        parser_calls += 1
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.parse_xlsx_with_timeout",
        forbidden_parser,
    )
    environment: dict[str, str]
    if case_id == "decompressed":
        content = build_xlsx(
            [[(1, ("编码",)), (2, ("A",))]],
            compression=zipfile.ZIP_STORED,
            extra_entries={"xl/media/padding.bin": b"x" * 4_096},
        )
        environment = {
            "import_max_decompressed_bytes": "1000",
            "import_max_compression_ratio": "100",
        }
        expected_code = "xlsx_decompressed_too_large"
        actual_key = "actual_decompressed_bytes"
    else:
        content = build_xlsx(
            [[(1, ("编码",)), (2, ("A",))]],
            extra_entries={"xl/media/padding.bin": b"A" * (1024 * 1024)},
        )
        environment = {
            "import_max_decompressed_bytes": str(2 * 1024 * 1024),
            "import_max_compression_ratio": "100",
        }
        expected_code = "xlsx_compression_ratio_too_high"
        actual_key = "actual_compression_ratio"
    async with running_clients(
        clean_import_database,
        tmp_path,
        monkeypatch,
        **environment,
    ) as (client, _):
        response = await post_import(client, content)
    assert response.status_code == 413
    assert response.json()["error_code"] == expected_code
    assert parser_calls == 0
    detail = cast(dict[str, object], audit_rows("failure")[-1][4])
    assert cast(float, detail[actual_key]) > 0


@pytest.mark.api
async def test_tc21_xxe_payload_causes_zero_external_requests(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY ext SYSTEM '
        b'"http://127.0.0.1:9/controlled">]><x>&ext;</x>'
    )
    content = build_xlsx(
        [[(1, ("编码",)), (2, ("A",))]],
        extra_entries={"xl/externalLinks/externalLink1.xml": payload},
    )
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        response = await post_import(client, content)
    assert response.status_code == 422
    assert response.json()["error_code"] == "unsafe_xlsx_xml"
    assert "controlled" not in response.text
    assert product_rows() == []


@pytest.mark.api
async def test_tc22_parse_timeout_is_audited_and_never_enters_phase_two(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def timeout_parser(
        content: bytes,
        *,
        timeout_seconds: float,
        max_rows: int,
    ) -> object:
        del content
        assert max_rows == 5_000
        raise XlsxRejected(
            "xlsx_parse_timeout",
            "XLSX 解析超过 30 秒上限, 已中止。",
            detail={
                "reason": "parse_timeout",
                "actual_elapsed_seconds": 30.1,
                "max_parse_seconds": timeout_seconds,
            },
            status_code=408,
        )

    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.parse_xlsx_with_timeout",
        timeout_parser,
    )
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        response = await post_import(
            client,
            sheet([(1, ("编码",)), (2, ("NEVER-WRITTEN",))]),
        )
    assert response.status_code == 408
    assert product_rows() == []
    detail = cast(dict[str, object], audit_rows("failure")[-1][4])
    assert detail["max_parse_seconds"] == 30
    assert detail["actual_elapsed_seconds"] == 30.1


@pytest.mark.api
async def test_tc24_two_concurrent_imports_share_unique_conflict_as_duplicate(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = create_product_in_transaction
    barrier = asyncio.Barrier(2)

    async def synchronized_create(
        connection: Connection,
        raw_code: str,
        raw_name: str,
        *,
        actor_id: int,
        request_id: UUID | None = None,
        audit_action: str | None = None,
    ) -> Product:
        if raw_code == "COMMON":
            await barrier.wait()
        return await original(
            connection,
            raw_code,
            raw_name,
            actor_id=actor_id,
            request_id=request_id,
            audit_action=audit_action,
        )

    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.create_product_in_transaction",
        synchronized_create,
    )
    first = sheet([(1, ("编码",)), (2, ("COMMON",)), (3, ("ONLY-A",))])
    second = sheet([(1, ("编码",)), (2, ("COMMON",)), (3, ("ONLY-B",))])
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        responses = await asyncio.gather(post_import(client, first), post_import(client, second))
    bodies = [response.json() for response in responses]
    assert all(response.status_code == 200 for response in responses)
    assert all(body["format_error_count"] == 0 for body in bodies)
    assert sum(body["success_count"] for body in bodies) == len(product_rows()) == 3
    assert sum(body["duplicate_count"] for body in bodies) == 1
    assert sum(row[0] == "COMMON" for row in product_rows()) == 1


@pytest.mark.api
async def test_tc25_manual_create_and_import_same_code_are_consistent(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import_create = create_product_in_transaction
    original_manual_create = create_product
    barrier = asyncio.Barrier(2)

    async def synchronized_import(
        connection: Connection,
        raw_code: str,
        raw_name: str,
        *,
        actor_id: int,
        request_id: UUID | None = None,
        audit_action: str | None = None,
    ) -> Product:
        if raw_code == "RACE":
            await barrier.wait()
        return await original_import_create(
            connection,
            raw_code,
            raw_name,
            actor_id=actor_id,
            request_id=request_id,
            audit_action=audit_action,
        )

    async def synchronized_manual(
        database: Database,
        raw_code: str,
        raw_name: str,
        *,
        actor_id: int,
        request_id: UUID | None = None,
    ) -> Product:
        if raw_code.strip().upper() == "RACE":
            await barrier.wait()
        return await original_manual_create(
            database,
            raw_code,
            raw_name,
            actor_id=actor_id,
            request_id=request_id,
        )

    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.create_product_in_transaction",
        synchronized_import,
    )
    monkeypatch.setattr(
        "product_pdf_qr.domains.product.router.create_product",
        synchronized_manual,
    )
    content = sheet([(1, ("编码",)), (2, ("RACE",)), (3, ("IMPORT-ONLY",))])
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        import_response, manual_response = await asyncio.gather(
            post_import(client, content),
            client.post("/api/products", json={"code": " race ", "name": "手工名称"}),
        )
    assert import_response.status_code == 200
    assert manual_response.status_code in {201, 409}
    assert sum(row[0] == "RACE" for row in product_rows()) == 1
    assert any(row[0] == "IMPORT-ONLY" for row in product_rows())
    assert import_response.json()["format_error_count"] == 0
    if manual_response.status_code == 201:
        assert import_response.json()["duplicate_count"] == 1


@pytest.mark.api
async def test_tc26_tc27_tc28_tc29_imported_token_security_statistics(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    rows = [(1, ("编码",))]
    rows.extend((index + 2, (f"T{index:04}",)) for index in range(5_000))
    content = sheet(rows)
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        first_response = await post_import(client, content)
        assert first_response.status_code == 200
        first_rows = product_rows()
        migration_url = required_environment("TEST_MIGRATION_DATABASE_URL").replace(
            "postgresql+psycopg://",
            "postgresql://",
        )
        with psycopg.connect(migration_url, autocommit=True) as connection:
            connection.execute("TRUNCATE products RESTART IDENTITY CASCADE")
            connection.execute("TRUNCATE audit_events RESTART IDENTITY")
        second_response = await post_import(client, content)
    assert first_response.json()["success_count"] == 5_000
    assert second_response.json()["success_count"] == 5_000
    second_rows = product_rows()
    first_mapping = {str(row[0]): str(row[2]) for row in first_rows}
    second_mapping = {str(row[0]): str(row[2]) for row in second_rows}
    assert len(first_mapping) == len(second_mapping) == 5_000
    assert len(set(first_mapping.values())) == len(set(second_mapping.values())) == 5_000
    assert all(
        len(token) == 26 and set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
        for token in first_mapping.values()
    )
    assert sum(first_mapping[code] == second_mapping[code] for code in first_mapping) == 0
    ordered_codes = sorted(first_mapping)
    ordered_tokens = [first_mapping[code] for code in ordered_codes]
    sorted_indexes = sorted(range(5_000), key=ordered_tokens.__getitem__)
    ranks = [0] * 5_000
    for rank, index in enumerate(sorted_indexes):
        ranks[index] = rank
    squared_difference = sum((index - rank) ** 2 for index, rank in enumerate(ranks))
    rho = 1 - (6 * squared_difference) / (5_000 * (5_000**2 - 1))
    rho_threshold = 5 / math.sqrt(4_999)
    assert abs(rho) <= rho_threshold
    sequences = [ordered_tokens]
    sequences.extend([token[:length] for token in ordered_tokens] for length in range(1, 7))
    for sequence in sequences:
        assert not all(left < right for left, right in pairwise(sequence))
        assert not all(left > right for left, right in pairwise(sequence))

    def lcp(left: str, right: str) -> int:
        return next(
            (
                index
                for index, (left_char, right_char) in enumerate(zip(left, right, strict=True))
                if left_char != right_char
            ),
            len(left),
        )

    adjacent_lcps = [
        lcp(ordered_tokens[index], ordered_tokens[index + 1]) for index in range(4_999)
    ]
    max_lcp = max(adjacent_lcps)
    max_lcp_index = adjacent_lcps.index(max_lcp)
    assert max_lcp <= 5
    pair_count = math.comb(5_000, 2)
    measured_collisions = {
        length: sum(
            count * (count - 1) // 2
            for count in Counter(token[:length] for token in ordered_tokens).values()
        )
        for length in range(1, 7)
    }
    expected_collisions = {length: pair_count / (32**length) for length in range(1, 7)}
    assert expected_collisions[3] == pytest.approx(381.3934, rel=1e-4)
    assert expected_collisions[5] < 1
    assert expected_collisions[6] == pytest.approx(0.01164, rel=1e-3)
    first_sample_sha = hashlib.sha256(
        "".join(f"{code}:{first_mapping[code]}\n" for code in ordered_codes).encode()
    ).hexdigest()
    second_sample_sha = hashlib.sha256(
        "".join(f"{code}:{second_mapping[code]}\n" for code in ordered_codes).encode()
    ).hexdigest()
    assert first_sample_sha != second_sample_sha
    record_property("token_sample_size", 5_000)
    record_property("same_code_token_matches", 0)
    record_property("first_mapping_sha256", first_sample_sha)
    record_property("second_mapping_sha256", second_sample_sha)
    record_property("spearman_rho", rho)
    record_property("spearman_five_sigma_threshold", rho_threshold)
    record_property("max_adjacent_lcp", max_lcp)
    record_property("max_adjacent_lcp_left_index", max_lcp_index)
    for length in range(1, 7):
        record_property(f"prefix_{length}_measured_pairs", measured_collisions[length])
        record_property(f"prefix_{length}_expected_pairs", expected_collisions[length])


@pytest.mark.api
async def test_tc30_only_token_conflict_row_retries(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occupied_bytes = bytes(range(16))
    occupied_token = base64.b32encode(occupied_bytes).decode("ascii").rstrip("=")
    runtime_url = required_environment("TEST_DATABASE_URL")
    with psycopg.connect(runtime_url) as connection:
        connection.execute(
            """
            INSERT INTO products (code, name, public_token, created_at, updated_at)
            VALUES ('OCCUPIED', '占用', %s, now(), now())
            """,
            (occupied_token,),
        )
        connection.commit()
    generated = iter([b"\x01" * 16, occupied_bytes, b"\x02" * 16, b"\x03" * 16])
    calls = 0

    def fake_token_bytes(length: int) -> bytes:
        nonlocal calls
        assert length == 16
        calls += 1
        return next(generated)

    content = sheet([(1, ("编码",)), (2, ("FIRST",)), (3, ("SECOND",)), (4, ("THIRD",))])
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        monkeypatch.setattr(
            "product_pdf_qr.domains.product.service.secrets.token_bytes",
            fake_token_bytes,
        )
        response = await post_import(client, content)
    assert response.status_code == 200
    assert response.json()["success_count"] == 3
    assert calls == 4
    assert len(product_rows()) == 4


@pytest.mark.api
async def test_tc31_token_retry_exhaustion_rolls_back_as_system_error(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occupied_bytes = bytes(range(16))
    occupied_token = base64.b32encode(occupied_bytes).decode("ascii").rstrip("=")
    with psycopg.connect(required_environment("TEST_DATABASE_URL")) as connection:
        connection.execute(
            """
            INSERT INTO products (code, name, public_token, created_at, updated_at)
            VALUES ('OCCUPIED', '占用', %s, now(), now())
            """,
            (occupied_token,),
        )
        connection.commit()
    calls = 0

    def fake_token_bytes(length: int) -> bytes:
        nonlocal calls
        calls += 1
        assert length == 16
        return occupied_bytes

    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        monkeypatch.setattr(
            "product_pdf_qr.domains.product.service.secrets.token_bytes",
            fake_token_bytes,
        )
        response = await post_import(
            client,
            sheet([(1, ("编码",)), (2, ("ROLLBACK-1",)), (3, ("ROLLBACK-2",))]),
        )
    body = response.json()
    assert response.status_code == 503
    assert body["error_code"] == "token_retry_exhausted"
    assert body["format_error_count"] == 0
    assert body["errors"][0]["kind"] == "system"
    assert calls == 5
    assert [row[0] for row in product_rows()] == ["OCCUPIED"]
    detail = cast(dict[str, object], audit_rows("failure")[-1][4])
    assert detail["system_error_code"] == "token_retry_exhausted"


@pytest.mark.api
async def test_tc32_success_and_failure_audits_use_result_specific_transactions(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        success = await post_import(client, sheet([(1, ("编码",)), (2, ("GOOD",))]))
        failure = await post_import(client, sheet([(1, ("编码",)), (2, ("BAD#",))]))
    assert success.status_code == 200
    assert failure.status_code == 422
    rows = audit_rows()
    assert [row[1] for row in rows] == ["success", "failure"]
    assert [row[0] for row in product_rows()] == ["GOOD"]
    assert cast(dict[str, object], rows[0][4])["success_count"] == 1
    assert cast(dict[str, object], rows[1][4])["format_error_count"] == 1


@pytest.mark.api
async def test_tc33_tc34_tc35_session_and_csrf_rejections_are_audited_before_parse(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = sheet([(1, ("编码",)), (2, ("NEVER",))])
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, anonymous):
        no_session = await anonymous.post(
            "/api/product-imports",
            files={"file": ("products.xlsx", content, XLSX_MIME)},
        )
        client.headers.pop(CSRF_HEADER_NAME)
        missing_csrf = await client.post(
            "/api/product-imports",
            files={"file": ("products.xlsx", content, XLSX_MIME)},
        )
        mismatch_csrf = await client.post(
            "/api/product-imports",
            files={"file": ("products.xlsx", content, XLSX_MIME)},
            headers={CSRF_HEADER_NAME: "wrong"},
        )
    assert no_session.status_code == 303
    assert missing_csrf.status_code == mismatch_csrf.status_code == 403
    assert product_rows() == []
    rows = audit_rows("failure")
    reasons = [cast(dict[str, object], row[4])["reason"] for row in rows]
    assert reasons == ["unauthenticated_rejection", "csrf_rejection", "csrf_rejection"]
    assert cast(dict[str, object], rows[1][4])["actual"] == "missing"
    assert cast(dict[str, object], rows[2][4])["actual"] == "mismatch"


@pytest.mark.e2e
async def test_tc36_tc37_import_page_is_protected_and_renders_success_counts(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = sheet([(1, ("编码",)), (2, ("PAGE",)), (3, ("page",))])
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, anonymous):
        assert (await anonymous.get("/admin/imports")).status_code == 303
        entry = await client.get("/admin/imports")
        result = await post_import_page(client, clean_import_database, content)
    assert entry.status_code == 200
    assert 'method="post"' in entry.text
    assert 'enctype="multipart/form-data"' in entry.text
    assert 'name="csrf_token"' in entry.text
    assert result.status_code == 200
    assert '<dd id="success-count">1</dd>' in result.text
    assert '<dd id="duplicate-count">1</dd>' in result.text
    assert '<dd id="format-error-count">0</dd>' in result.text


@pytest.mark.api
@pytest.mark.e2e
async def test_tc38_3000_errors_are_all_returned_and_server_rendered(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [(1, ("编码",))]
    rows.extend((index + 2, (f"BAD#{index}",)) for index in range(3_000))
    content = sheet(rows)
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        api_response = await post_import(client, content)
        page_response = await post_import_page(client, clean_import_database, content)
    body = api_response.json()
    assert api_response.status_code == page_response.status_code == 422
    assert (body["success_count"], body["duplicate_count"], body["format_error_count"]) == (
        0,
        0,
        3_000,
    )
    assert len(body["errors"]) == 3_000
    assert {error["row"] for error in body["errors"]} == set(range(2, 3_002))
    assert page_response.text.count("data-import-error") == 3_000
    assert '<dd id="format-error-count">3000</dd>' in page_response.text
    assert product_rows() == []


@pytest.mark.api
@pytest.mark.e2e
async def test_tc39_tc40_multiple_nonempty_sheets_notice_and_blank_sheet_boundary(
    clean_import_database: ImportAuth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    three_sheets: list[Worksheet] = [
        [(1, ("编码",)), (2, ("FIRST-1",)), (3, ("FIRST-2",))],
        [(1, ("编码",)), (2, ("SECOND-ONLY",))],
        [(1, ("编码",)), (2, ("THIRD-ONLY",))],
    ]
    blank_extras: list[Worksheet] = [
        [(1, ("编码",)), (2, ("BLANK-BOUNDARY",))],
        [],
        [(1, (" ",)), (2, ("\t",))],
    ]
    async with running_clients(clean_import_database, tmp_path, monkeypatch) as (client, _):
        first_response = await post_import_page(
            client,
            clean_import_database,
            build_xlsx(three_sheets),
        )
        blank_response = await post_import_page(
            client,
            clean_import_database,
            build_xlsx(blank_extras),
        )
    notice = "本文件含 3 个工作表，仅导入第 1 个"  # noqa: RUF001
    assert notice in first_response.text
    assert notice not in blank_response.text
    codes = {row[0] for row in product_rows()}
    assert codes == {"FIRST-1", "FIRST-2", "BLANK-BOUNDARY"}
    assert {"SECOND-ONLY", "THIRD-ONLY"}.isdisjoint(codes)
