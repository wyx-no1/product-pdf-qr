"""Direct management-handler tests for business and compensation responses."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Never

import pytest
from fastapi import UploadFile
from psycopg.types.json import Jsonb
from pypdf import PdfWriter
from starlette.datastructures import Headers

from product_pdf_qr.dependencies import (
    get_database,
    get_public_miss_limiter,
    get_qrcode_service,
    get_runtime_settings,
    get_storage_service,
)
from product_pdf_qr.domains.product.router import (
    ProductCreateRequest,
    create_product_endpoint,
    download_qrcode,
    get_product_endpoint,
    list_products_endpoint,
    retry_qrcode,
    upload_pdf_endpoint,
)
from product_pdf_qr.domains.public import PublicMissLimiter
from product_pdf_qr.domains.qrcode import QRCodeService
from product_pdf_qr.domains.qrcode.router import report_qrcode_failures
from product_pdf_qr.domains.storage import StorageService, UploadRejected
from product_pdf_qr.domains.storage.router import report_orphan_files
from product_pdf_qr.errors import AppError
from tests.unit.test_business_services import (
    ScriptedConnection,
    ScriptedCursor,
    ScriptedDatabase,
    as_database,
)


def product_row() -> dict[str, object]:
    now = datetime(2026, 8, 4, 9, 30, tzinfo=UTC)
    return {
        "id": 5,
        "code": "A001",
        "name": "测试产品",
        "public_token": "A" * 26,
        "status": "active",
        "current_version_id": None,
        "created_at": now,
        "updated_at": now,
    }


def synthetic_upload() -> UploadFile:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return UploadFile(
        file=io.BytesIO(output.getvalue()),
        filename="../../safe-storage.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )


@pytest.mark.anyio
async def test_create_download_and_retry_qrcode_handlers(tmp_path: Path) -> None:
    qrcode_service = QRCodeService(tmp_path, "http://127.0.0.1:8000")
    create_database = as_database(ScriptedDatabase(ScriptedConnection([product_row(), None])))

    created = await create_product_endpoint(
        ProductCreateRequest(code=" a001 ", name=" 测试产品 "),
        create_database,
        qrcode_service,
    )

    assert created.code == "A001"
    assert created.name == "测试产品"
    assert created.public_url.endswith("/p/" + ("A" * 26))
    assert created.qrcode_status == "ready"

    download_database = as_database(ScriptedDatabase(ScriptedConnection([product_row()])))
    response = await download_qrcode(5, download_database, qrcode_service)
    assert response.media_type == "image/png"
    assert response.headers["content-disposition"] == 'attachment; filename="A001.png"'

    retry_database = as_database(
        ScriptedDatabase(
            ScriptedConnection([product_row()]),
            ScriptedConnection([None]),
        )
    )
    retried = await retry_qrcode(5, retry_database, qrcode_service)
    assert retried.status == "ready"
    assert retried.cache_hit


@pytest.mark.anyio
async def test_create_survives_qrcode_generation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qrcode_service = QRCodeService(tmp_path, "http://127.0.0.1:8000")

    def fail_generate(_code: str, _token: str) -> bytes:
        from product_pdf_qr.domains.qrcode import QRCodeGenerationError

        raise QRCodeGenerationError("synthetic generation failure")

    monkeypatch.setattr(qrcode_service, "generate", fail_generate)
    database = as_database(
        ScriptedDatabase(
            ScriptedConnection([product_row(), None]),
            ScriptedConnection([None]),
        )
    )

    created = await create_product_endpoint(
        ProductCreateRequest(code="A001", name="测试产品"),
        database,
        qrcode_service,
    )

    assert created.id == 5
    assert created.qrcode_status == "generation_failed"
    assert not (qrcode_service.cache_root / "A001.png").exists()


@pytest.mark.anyio
async def test_list_and_detail_handlers_expose_persisted_state(tmp_path: Path) -> None:
    historical = {**product_row(), "id": 4, "code": "OLD", "name": None}
    uploaded = {**product_row(), "current_version_id": 17}
    list_database = as_database(ScriptedDatabase(ScriptedConnection([[uploaded, historical]])))

    products = await list_products_endpoint(list_database, limit=25, offset=5)

    assert [product.name for product in products] == ["测试产品", None]
    assert [product.pdf_status for product in products] == ["uploaded", "not_uploaded"]

    qrcode_service = QRCodeService(tmp_path, "http://127.0.0.1:8000")
    await qrcode_service.get_or_generate("A001", "A" * 26)
    detail_database = as_database(ScriptedDatabase(ScriptedConnection([uploaded])))

    detail = await get_product_endpoint(5, detail_database, qrcode_service)

    assert detail.name == "测试产品"
    assert detail.pdf_status == "uploaded"
    assert detail.qrcode_status == "ready"
    assert detail.public_url.endswith("/p/" + ("A" * 26))


@pytest.mark.anyio
async def test_upload_handler_validates_then_creates_version(tmp_path: Path) -> None:
    storage = StorageService(tmp_path / "storage", max_pdf_bytes=1024 * 1024)

    # The exact writer length is environment-stable but the scripted metadata must
    # be derived from the validated bytes, so handle this endpoint with a connection
    # that fills file metadata after validation.
    class DynamicConnection(ScriptedConnection):
        async def execute(self, query: str, params: object = None) -> ScriptedCursor:
            normalized = " ".join(query.split())
            self.queries.append(normalized)
            self.parameters.append(params)
            if "FROM products" in normalized and "FOR UPDATE" in normalized:
                return ScriptedCursor({"id": 5, "code": "A001", "current_version_id": None})
            if "FROM admins" in normalized:
                return ScriptedCursor({"id": 9})
            if "MAX(version_no)" in normalized:
                return ScriptedCursor({"next_version_no": 1})
            if "INSERT INTO pdf_files" in normalized:
                values = params
                assert isinstance(values, tuple)
                return ScriptedCursor(
                    {
                        "id": 11,
                        "size_bytes": values[1],
                        "storage_path": values[2],
                    }
                )
            if "INSERT INTO pdf_versions" in normalized:
                return ScriptedCursor({"id": 13})
            return ScriptedCursor()

    connection = DynamicConnection([])
    database = as_database(ScriptedDatabase(connection))

    uploaded = await upload_pdf_endpoint(
        5,
        9,
        synthetic_upload(),
        database,
        storage,
    )

    assert uploaded.version_id == 13
    assert uploaded.version_no == 1
    assert uploaded.original_filename == "../../safe-storage.pdf"
    assert list(storage.files_root.rglob("*.pdf"))


@pytest.mark.anyio
async def test_upload_validation_rejection_is_audited(tmp_path: Path) -> None:
    storage = StorageService(tmp_path, max_pdf_bytes=1024)
    audit_connection = ScriptedConnection([None])
    database = as_database(ScriptedDatabase(audit_connection))
    invalid = UploadFile(
        file=io.BytesIO(b"not-pdf"),
        filename="bad.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )

    with pytest.raises(UploadRejected):
        await upload_pdf_endpoint(5, 9, invalid, database, storage)

    audit_parameters = audit_connection.parameters[0]
    assert isinstance(audit_parameters, tuple)
    audit_detail = audit_parameters[-1]
    assert isinstance(audit_detail, Jsonb)
    assert audit_detail.obj == {"reason": "invalid_pdf_signature", "stage": "signature"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("code", "stage"),
    [
        ("pdf_validation_timeout", "structure_timeout"),
        ("pdf_validation_resource_limit", "structure_resource"),
    ],
)
async def test_parser_limit_rejection_reason_is_audited(
    code: str,
    stage: str,
) -> None:
    class ParserRejectingStorage(StorageService):
        async def receive_and_validate(self, _upload: UploadFile) -> Never:
            raise UploadRejected(code, "PDF 结构校验失败。", stage)

    audit_connection = ScriptedConnection([None])
    database = as_database(ScriptedDatabase(audit_connection))

    with pytest.raises(UploadRejected):
        await upload_pdf_endpoint(
            5,
            9,
            synthetic_upload(),
            database,
            ParserRejectingStorage(Path("unused"), max_pdf_bytes=1024),
        )

    audit_parameters = audit_connection.parameters[0]
    assert isinstance(audit_parameters, tuple)
    audit_detail = audit_parameters[-1]
    assert isinstance(audit_detail, Jsonb)
    assert audit_detail.obj == {"reason": code, "stage": stage}


@pytest.mark.anyio
async def test_product_lookup_missing_is_safe(tmp_path: Path) -> None:
    database = as_database(ScriptedDatabase(ScriptedConnection([None])))

    with pytest.raises(AppError) as captured:
        await download_qrcode(
            999,
            database,
            QRCodeService(tmp_path, "http://127.0.0.1:8000"),
        )

    assert captured.value.code == "product_not_found"


@pytest.mark.anyio
async def test_specialized_read_only_reports(tmp_path: Path) -> None:
    qrcode_service = QRCodeService(tmp_path, "http://127.0.0.1:8000")
    failure_rows = [
        {
            "target_id": 5,
            "product_code": "A001",
            "detail": {"reason": "synthetic"},
        }
    ]
    failures = await report_qrcode_failures(
        as_database(ScriptedDatabase(ScriptedConnection([failure_rows]))),
        qrcode_service,
    )
    assert failures[0].product_code == "A001"
    assert failures[0].reason == "synthetic"

    storage = StorageService(tmp_path / "storage", max_pdf_bytes=1024)
    storage.prepare()
    relative = storage.relative_path_for_hash("e" * 64)
    orphan = storage.files_root / relative
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    report = await report_orphan_files(
        as_database(ScriptedDatabase(ScriptedConnection([[]]))),
        storage,
    )
    assert [item.storage_path for item in report] == [relative]
    assert orphan.is_file()


def test_dependency_accessors_return_application_state() -> None:
    values = {
        "database": object(),
        "settings": object(),
        "storage_service": object(),
        "qrcode_service": object(),
        "public_miss_limiter": PublicMissLimiter(1, 1),
    }
    app = SimpleNamespace(state=SimpleNamespace(**values))
    request = SimpleNamespace(app=app)

    assert get_database(request) is values["database"]  # type: ignore[arg-type]
    assert get_runtime_settings(request) is values["settings"]  # type: ignore[arg-type]
    assert get_storage_service(request) is values["storage_service"]  # type: ignore[arg-type]
    assert get_qrcode_service(request) is values["qrcode_service"]  # type: ignore[arg-type]
    assert get_public_miss_limiter(request) is values["public_miss_limiter"]  # type: ignore[arg-type]
