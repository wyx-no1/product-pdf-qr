"""Database-facing business services with deterministic scripted connections."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from psycopg.types.json import Jsonb

from product_pdf_qr.database import Connection, Database
from product_pdf_qr.domains.audit import AuditEvent, append_event, append_independent_event
from product_pdf_qr.domains.product import (
    create_product,
    get_product,
    list_products,
    normalize_product_name,
    set_product_status,
)
from product_pdf_qr.domains.public import resolve_public_document
from product_pdf_qr.domains.storage import PublishedFile, StorageService, ValidatedUpload
from product_pdf_qr.domains.version import (
    DuplicateCurrentPDF,
    list_pdf_versions,
    restore_pdf_version,
    upload_pdf,
)
from product_pdf_qr.errors import AppError


class ScriptedCursor:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.row = row
        self.rows = rows or []

    async def fetchone(self) -> dict[str, object] | None:
        return self.row

    async def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class ScriptedConnection:
    def __init__(
        self,
        results: Sequence[dict[str, object] | list[dict[str, object]] | None],
        *,
        commit_error: Exception | None = None,
        execute_error: Exception | None = None,
    ) -> None:
        self.results = list(results)
        self.commit_error = commit_error
        self.execute_error = execute_error
        self.queries: list[str] = []
        self.parameters: list[object] = []

    async def execute(self, query: str, params: object = None) -> ScriptedCursor:
        self.queries.append(" ".join(query.split()))
        self.parameters.append(params)
        if self.execute_error is not None:
            raise self.execute_error
        result = self.results.pop(0) if self.results else None
        if isinstance(result, list):
            return ScriptedCursor(rows=result)
        return ScriptedCursor(row=result)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        try:
            yield
        except BaseException:
            raise
        else:
            if self.commit_error is not None:
                raise self.commit_error


class ScriptedDatabase:
    def __init__(self, *connections: ScriptedConnection) -> None:
        self.connections = list(connections)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[ScriptedConnection]:
        if not self.connections:
            raise RuntimeError("No scripted connection remains")
        yield self.connections.pop(0)


def as_database(database: ScriptedDatabase) -> Database:
    return cast(Database, database)


def as_connection(connection: ScriptedConnection) -> Connection:
    return cast(Connection, connection)


@pytest.mark.anyio
async def test_append_event_and_independent_failure_handling() -> None:
    connection = ScriptedConnection([None])
    event = AuditEvent(
        action="product_create",
        result="success",
        target_type="product",
        target_id=7,
        product_code="A001",
        detail={"safe": True},
    )

    await append_event(as_connection(connection), event)

    assert "INSERT INTO audit_events" in connection.queries[0]
    assert "A001" in cast(tuple[object, ...], connection.parameters[0])

    failing = ScriptedDatabase(
        ScriptedConnection([], execute_error=RuntimeError("synthetic audit failure"))
    )
    assert not await append_independent_event(as_database(failing), event)


@pytest.mark.anyio
async def test_create_product_commits_audit_with_normalized_code() -> None:
    connection = ScriptedConnection(
        [
            {
                "id": 7,
                "code": "A001",
                "name": "测试产品",
                "public_token": "A" * 26,
                "status": "active",
                "current_version_id": None,
            },
            None,
        ]
    )

    product = await create_product(
        as_database(ScriptedDatabase(connection)),
        " a001 ",
        " 测试产品 ",
        actor_id=9,
    )

    assert product.code == "A001"
    assert product.name == "测试产品"
    assert product.current_version_id is None
    assert "INSERT INTO products" in connection.queries[0]
    assert cast(tuple[object, ...], connection.parameters[0])[:2] == ("A001", "测试产品")
    assert "INSERT INTO audit_events" in connection.queries[1]
    audit_parameters = cast(tuple[object, ...], connection.parameters[1])
    assert audit_parameters[:2] == ("admin", 9)
    assert audit_parameters[2] == "product_create"


@pytest.mark.anyio
async def test_create_product_rejects_duplicate_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SyntheticUniqueViolation(Exception):
        def __init__(self) -> None:
            self.diag = SimpleNamespace(constraint_name="products_code_key")

    connection = ScriptedConnection([], execute_error=SyntheticUniqueViolation())
    monkeypatch.setattr(
        "product_pdf_qr.domains.product.service.UniqueViolation",
        SyntheticUniqueViolation,
    )

    with pytest.raises(AppError) as captured:
        await create_product(
            as_database(ScriptedDatabase(connection)),
            "A001",
            "测试产品",
            actor_id=9,
        )

    assert captured.value.code == "duplicate_product_code"
    assert captured.value.status_code == 409


@pytest.mark.parametrize("name", ["", "   ", "A" * 121])
def test_product_name_validation_is_bounded(name: str) -> None:
    with pytest.raises(AppError) as captured:
        normalize_product_name(name)

    assert captured.value.code == "invalid_product_name"
    assert captured.value.status_code == 422


@pytest.mark.anyio
async def test_product_list_and_detail_support_historical_null_names() -> None:
    updated = datetime(2026, 8, 4, 9, 30, tzinfo=UTC)
    historical = {
        "id": 4,
        "code": "HISTORICAL",
        "name": None,
        "public_token": "H" * 26,
        "status": "active",
        "current_version_id": None,
        "created_at": updated,
        "updated_at": updated,
    }
    current = {
        **historical,
        "id": 5,
        "code": "CURRENT",
        "name": "当前产品",
        "public_token": "C" * 26,
        "current_version_id": 11,
    }
    list_connection = ScriptedConnection([[current, historical]])
    detail_connection = ScriptedConnection([historical])
    database = as_database(ScriptedDatabase(list_connection, detail_connection))

    products = await list_products(database, limit=20, offset=10)
    product = await get_product(database, 4)

    assert [item.name for item in products] == ["当前产品", None]
    assert product.name is None
    assert product.updated_at == updated
    assert "ORDER BY updated_at DESC, id DESC" in list_connection.queries[0]
    assert list_connection.parameters[0] == (20, 10)
    assert detail_connection.parameters[0] == (4,)


@pytest.mark.anyio
async def test_product_list_filters_in_sql_before_pagination_and_escapes_like() -> None:
    search_connection = ScriptedConnection([[]])
    uploaded_connection = ScriptedConnection([[]])
    not_uploaded_connection = ScriptedConnection([[]])
    database = as_database(
        ScriptedDatabase(
            search_connection,
            uploaded_connection,
            not_uploaded_connection,
        )
    )

    await list_products(
        database,
        limit=5,
        offset=10,
        q=r" v1%_\legacy ",
    )
    await list_products(
        database,
        limit=20,
        offset=0,
        pdf_status="uploaded",
    )
    await list_products(
        database,
        limit=20,
        offset=20,
        pdf_status="not_uploaded",
    )

    search_sql = search_connection.queries[0]
    assert "code ILIKE %s" in search_sql
    assert "COALESCE(name, '') ILIKE %s" in search_sql
    assert search_sql.index("WHERE") < search_sql.index("ORDER BY")
    assert search_sql.index("ORDER BY") < search_sql.index("LIMIT")
    assert search_connection.parameters[0] == (
        r"%v1\%\_\\legacy%",
        r"%v1\%\_\\legacy%",
        5,
        10,
    )
    assert "current_version_id IS NOT NULL" in uploaded_connection.queries[0]
    assert uploaded_connection.parameters[0] == (20, 0)
    assert "current_version_id IS NULL" in not_uploaded_connection.queries[0]
    assert not_uploaded_connection.parameters[0] == (20, 20)


@pytest.mark.anyio
async def test_get_product_missing_uses_safe_error() -> None:
    database = as_database(ScriptedDatabase(ScriptedConnection([None])))

    with pytest.raises(AppError) as captured:
        await get_product(database, 999)

    assert captured.value.code == "product_not_found"
    assert captured.value.status_code == 404


@pytest.mark.anyio
async def test_product_status_transition_is_locked_and_audited() -> None:
    previous = {
        "id": 5,
        "code": "A001",
        "name": "测试产品",
        "public_token": "A" * 26,
        "status": "active",
        "current_version_id": 13,
        "created_at": datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
    }
    updated = {
        **previous,
        "status": "disabled",
        "updated_at": datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
    }
    connection = ScriptedConnection([previous, updated, None])

    product = await set_product_status(
        as_database(ScriptedDatabase(connection)),
        5,
        "disabled",
        actor_id=9,
    )

    assert product.status == "disabled"
    assert "FOR UPDATE" in connection.queries[0]
    assert "SET status = %s" in connection.queries[1]
    audit_parameters = cast(tuple[object, ...], connection.parameters[2])
    assert audit_parameters[:3] == ("admin", 9, "product_disable")
    audit_detail = cast(Jsonb, audit_parameters[-1]).obj
    assert audit_detail == {"previous_status": "active", "status": "disabled"}


@pytest.mark.anyio
async def test_setting_existing_product_status_is_idempotent() -> None:
    existing = {
        "id": 5,
        "code": "A001",
        "name": "测试产品",
        "public_token": "A" * 26,
        "status": "active",
        "current_version_id": None,
        "created_at": datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
    }
    connection = ScriptedConnection([existing])

    product = await set_product_status(
        as_database(ScriptedDatabase(connection)),
        5,
        "active",
        actor_id=9,
    )

    assert product.status == "active"
    assert len(connection.queries) == 1


@pytest.mark.anyio
async def test_version_history_marks_current_and_restore_only_moves_pointer() -> None:
    uploaded_at = datetime(2026, 8, 4, 9, 30, tzinfo=UTC)
    history_connection = ScriptedConnection(
        [
            {"current_version_id": 13},
            [
                {
                    "id": 14,
                    "product_id": 5,
                    "version_no": 2,
                    "original_filename": "second.pdf",
                    "uploaded_by": 9,
                    "uploaded_by_username": "owner",
                    "uploaded_at": uploaded_at,
                    "is_current": False,
                },
                {
                    "id": 13,
                    "product_id": 5,
                    "version_no": 1,
                    "original_filename": "first.pdf",
                    "uploaded_by": 9,
                    "uploaded_by_username": "owner",
                    "uploaded_at": uploaded_at,
                    "is_current": True,
                },
            ],
        ]
    )
    restore_connection = ScriptedConnection(
        [
            {"id": 5, "code": "A001", "current_version_id": 14},
            {"id": 13, "version_no": 1},
            None,
            None,
        ]
    )
    database = as_database(ScriptedDatabase(history_connection, restore_connection))

    versions = await list_pdf_versions(database, 5)
    restored = await restore_pdf_version(
        database,
        product_id=5,
        version_id=13,
        actor_id=9,
    )

    assert [version.version_no for version in versions] == [2, 1]
    assert [version.is_current for version in versions] == [False, True]
    assert restored.version_id == 13
    assert restored.version_no == 1
    assert "FOR UPDATE" in restore_connection.queries[0]
    assert restore_connection.parameters[1] == (5, 13)
    assert "UPDATE products" in restore_connection.queries[2]
    assert not any("INSERT INTO pdf_versions" in query for query in restore_connection.queries)
    assert not any("DELETE FROM pdf_versions" in query for query in restore_connection.queries)
    audit_parameters = cast(tuple[object, ...], restore_connection.parameters[3])
    assert audit_parameters[:3] == ("admin", 9, "pdf_restore")


@pytest.mark.anyio
async def test_version_history_and_restore_reject_missing_ownership() -> None:
    history_database = as_database(ScriptedDatabase(ScriptedConnection([None])))
    with pytest.raises(AppError) as missing_product:
        await list_pdf_versions(history_database, 999)
    assert missing_product.value.code == "product_not_found"

    restore_connection = ScriptedConnection(
        [
            {"id": 5, "code": "A001", "current_version_id": 14},
            None,
        ]
    )
    with pytest.raises(AppError) as wrong_version:
        await restore_pdf_version(
            as_database(ScriptedDatabase(restore_connection)),
            product_id=5,
            version_id=99,
            actor_id=9,
        )
    assert wrong_version.value.code == "version_not_found_for_product"
    assert "不属于该产品" in wrong_version.value.message
    assert not any("UPDATE products" in query for query in restore_connection.queries)


def validated_upload(tmp_path: Path, content: bytes = b"%PDF-synthetic") -> ValidatedUpload:
    path = tmp_path / "candidate.part"
    path.write_bytes(content)
    return ValidatedUpload(
        temporary_path=path,
        original_filename="../../original.pdf",
        size_bytes=len(content),
        sha256="a" * 64,
    )


@pytest.mark.anyio
async def test_upload_appends_version_moves_pointer_and_audits(tmp_path: Path) -> None:
    storage = StorageService(tmp_path / "storage", max_pdf_bytes=1024)
    upload = validated_upload(tmp_path)
    connection = ScriptedConnection(
        [
            {"id": 5, "code": "A001", "current_version_id": None},
            {"id": 9},
            {"next_version_no": 1},
            {
                "id": 11,
                "size_bytes": upload.size_bytes,
                "storage_path": "aa/aa/" + "a" * 64 + ".pdf",
            },
            {"id": 13},
            None,
            None,
        ]
    )

    version = await upload_pdf(
        as_database(ScriptedDatabase(connection)),
        storage,
        product_id=5,
        actor_id=9,
        upload=upload,
    )

    assert version.id == 13
    assert version.version_no == 1
    assert version.original_filename == "../../original.pdf"
    assert (storage.files_root / version.storage_path).read_bytes() == b"%PDF-synthetic"
    assert "FOR UPDATE" in connection.queries[0]
    assert "SELECT COALESCE(MAX(version_no)" in connection.queries[2]
    assert "UPDATE products" in connection.queries[5]
    assert "INSERT INTO audit_events" in connection.queries[6]


@pytest.mark.anyio
async def test_locked_current_duplicate_is_rejected_without_storage_side_effect(
    tmp_path: Path,
) -> None:
    upload = validated_upload(tmp_path)
    business = ScriptedConnection(
        [
            {"id": 5, "code": "A001", "current_version_id": 12},
            {"id": 9},
            {"size_bytes": upload.size_bytes, "sha256": upload.sha256},
        ]
    )
    rejection_audit = ScriptedConnection([None])
    database = ScriptedDatabase(business, rejection_audit)
    storage = StorageService(tmp_path / "storage", max_pdf_bytes=1024)

    with pytest.raises(DuplicateCurrentPDF):
        await upload_pdf(
            as_database(database),
            storage,
            product_id=5,
            actor_id=9,
            upload=upload,
        )

    assert not upload.temporary_path.exists()
    assert not storage.files_root.exists()
    assert "FOR UPDATE" in business.queries[0]
    assert "JOIN pdf_files" in business.queries[2]
    assert "pdf_upload_rejected" in cast(tuple[object, ...], rejection_audit.parameters[0])


@pytest.mark.anyio
async def test_commit_failure_leaves_traceable_orphan_not_database_result(
    tmp_path: Path,
) -> None:
    storage = StorageService(tmp_path / "storage", max_pdf_bytes=1024)
    upload = validated_upload(tmp_path)
    relative = "aa/aa/" + "a" * 64 + ".pdf"
    business = ScriptedConnection(
        [
            {"id": 5, "code": "A001", "current_version_id": None},
            {"id": 9},
            {"next_version_no": 1},
            {"id": 11, "size_bytes": upload.size_bytes, "storage_path": relative},
            {"id": 13},
            None,
            None,
        ],
        commit_error=RuntimeError("synthetic commit failure"),
    )
    rejection_audit = ScriptedConnection([None])

    with pytest.raises(AppError) as captured:
        await upload_pdf(
            as_database(ScriptedDatabase(business, rejection_audit)),
            storage,
            product_id=5,
            actor_id=9,
            upload=upload,
        )

    assert captured.value.code == "pdf_upload_failed"
    assert (storage.files_root / relative).is_file()
    audit_parameters = cast(tuple[object, ...], rejection_audit.parameters[0])
    audit_detail = cast(Jsonb, audit_parameters[-1]).obj
    assert isinstance(audit_detail, dict)
    assert audit_detail["moved_file_sha256"] == upload.sha256


@pytest.mark.anyio
async def test_cancellation_before_publish_cleans_candidate_and_is_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = StorageService(tmp_path / "storage", max_pdf_bytes=1024)
    candidate = validated_upload(tmp_path)
    business = ScriptedConnection(
        [
            {"id": 5, "code": "A001", "current_version_id": None},
            {"id": 9},
            {"next_version_no": 1},
        ]
    )
    rejection_audit = ScriptedConnection([None])
    entered_publish = asyncio.Event()

    async def block_before_publish(_upload: ValidatedUpload) -> None:
        entered_publish.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(storage, "publish", block_before_publish)
    task = asyncio.create_task(
        upload_pdf(
            as_database(ScriptedDatabase(business, rejection_audit)),
            storage,
            product_id=5,
            actor_id=9,
            upload=candidate,
        )
    )
    await entered_publish.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not candidate.temporary_path.exists()
    assert list(storage.files_root.rglob("*.pdf")) == []
    assert not any("INSERT INTO pdf_versions" in query for query in business.queries)
    audit_detail = cast(Jsonb, cast(tuple[object, ...], rejection_audit.parameters[0])[-1]).obj
    assert isinstance(audit_detail, dict)
    assert audit_detail["reason"] == "upload_cancelled"
    assert "moved_file_sha256" not in audit_detail


@pytest.mark.anyio
async def test_cancellation_after_publish_leaves_audited_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = StorageService(tmp_path / "storage", max_pdf_bytes=1024)
    storage.prepare()
    candidate = validated_upload(tmp_path)
    business = ScriptedConnection(
        [
            {"id": 5, "code": "A001", "current_version_id": None},
            {"id": 9},
            {"next_version_no": 1},
        ]
    )
    rejection_audit = ScriptedConnection([None])
    database = as_database(ScriptedDatabase(business, rejection_audit))
    publication_finished = asyncio.Event()
    release_publication = threading.Event()
    loop = asyncio.get_running_loop()
    original_publish_sync = storage._publish_sync

    def controlled_publish(upload: ValidatedUpload) -> PublishedFile:
        published = original_publish_sync(upload)
        loop.call_soon_threadsafe(publication_finished.set)
        if not release_publication.wait(timeout=2):
            raise RuntimeError("test did not release publication")
        return published

    monkeypatch.setattr(storage, "_publish_sync", controlled_publish)
    task = asyncio.create_task(
        upload_pdf(
            database,
            storage,
            product_id=5,
            actor_id=9,
            upload=candidate,
        )
    )
    await asyncio.wait_for(publication_finished.wait(), timeout=2)
    task.cancel()
    release_publication.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    orphan = storage.files_root / storage.relative_path_for_hash(candidate.sha256)
    assert not candidate.temporary_path.exists()
    assert orphan.read_bytes() == b"%PDF-synthetic"
    assert not any("INSERT INTO pdf_versions" in query for query in business.queries)
    audit_detail = cast(Jsonb, cast(tuple[object, ...], rejection_audit.parameters[0])[-1]).obj
    assert isinstance(audit_detail, dict)
    assert audit_detail["reason"] == "upload_cancelled"
    assert audit_detail["moved_file_sha256"] == candidate.sha256


@pytest.mark.anyio
async def test_existing_historical_content_reuses_file_and_creates_version(
    tmp_path: Path,
) -> None:
    storage = StorageService(tmp_path / "storage", max_pdf_bytes=1024)
    upload = validated_upload(tmp_path)
    storage.prepare()
    relative = storage.relative_path_for_hash(upload.sha256)
    target = storage.files_root / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF-synthetic")
    before_mtime = target.stat().st_mtime_ns
    connection = ScriptedConnection(
        [
            {"id": 5, "code": "A001", "current_version_id": 20},
            {"id": 9},
            {"size_bytes": 999, "sha256": "b" * 64},
            {"next_version_no": 3},
            None,
            {"id": 11, "size_bytes": upload.size_bytes, "storage_path": relative},
            {"id": 21},
            None,
            None,
        ]
    )

    version = await upload_pdf(
        as_database(ScriptedDatabase(connection)),
        storage,
        product_id=5,
        actor_id=9,
        upload=upload,
    )

    assert version.version_no == 3
    assert target.stat().st_mtime_ns == before_mtime
    assert "ON CONFLICT (sha256) DO NOTHING" in connection.queries[4]
    assert "SELECT id, size_bytes, storage_path" in connection.queries[5]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, "missing"),
        ({"status": "disabled", "current_version_id": None}, "disabled"),
        ({"status": "active", "current_version_id": None}, "unuploaded"),
    ],
)
async def test_public_state_ordering(
    tmp_path: Path,
    row: dict[str, object] | None,
    expected: str,
) -> None:
    database = ScriptedDatabase(ScriptedConnection([row]))
    storage = StorageService(tmp_path, max_pdf_bytes=1024)

    document = await resolve_public_document(as_database(database), storage, "A" * 26)

    assert document.state == expected


@pytest.mark.anyio
async def test_public_available_resolves_bounded_formal_file(tmp_path: Path) -> None:
    storage = StorageService(tmp_path, max_pdf_bytes=1024)
    storage.prepare()
    relative = storage.relative_path_for_hash("c" * 64)
    path = storage.files_root / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-public")
    row = {
        "status": "active",
        "current_version_id": 3,
        "original_filename": "资料.pdf",
        "storage_path": relative,
        "size_bytes": path.stat().st_size,
    }

    document = await resolve_public_document(
        as_database(ScriptedDatabase(ScriptedConnection([row]))),
        storage,
        "A" * 26,
    )

    assert document.state == "available"
    assert document.path == path.resolve()
    assert document.original_filename == "资料.pdf"
