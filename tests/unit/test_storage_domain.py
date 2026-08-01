"""PDF isolation, validation, publication, and reconciliation tests."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi import UploadFile
from pypdf import PdfWriter
from starlette.datastructures import Headers

from product_pdf_qr.domains.storage import StorageService, UploadRejected


def synthetic_pdf() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return output.getvalue()


def upload_file(
    content: bytes,
    *,
    filename: str = "synthetic.pdf",
    content_type: str = "application/pdf",
) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


class InterruptedUpload(UploadFile):
    """Synthetic client disconnect after one partial chunk."""

    def __init__(self) -> None:
        super().__init__(
            file=io.BytesIO(b"unused"),
            filename="interrupted.pdf",
            headers=Headers({"content-type": "application/pdf"}),
        )
        self.read_count = 0

    async def read(self, size: int = -1) -> bytes:
        self.read_count += 1
        if self.read_count == 1:
            return b"%PDF-partial"
        raise ConnectionError("synthetic upload interruption")


@pytest.mark.anyio
async def test_valid_pdf_isolated_hashed_and_path_ignores_filename(tmp_path: Path) -> None:
    storage = StorageService(tmp_path, max_pdf_bytes=1024 * 1024)

    validated = await storage.receive_and_validate(
        upload_file(synthetic_pdf(), filename="../../unsafe.pdf")
    )

    assert validated.temporary_path.parent == storage.temporary_root
    assert validated.original_filename == "../../unsafe.pdf"
    assert len(validated.sha256) == 64
    assert validated.size_bytes > 0
    assert ".." not in storage.relative_path_for_hash(validated.sha256)
    validated.discard()
    assert not validated.temporary_path.exists()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "filename", "content_type", "code", "stage"),
    [
        (b"", "empty.pdf", "application/pdf", "empty_pdf", "size"),
        (b"%PDF-bad", "wrong.txt", "application/pdf", "invalid_pdf_extension", "extension"),
        (b"%PDF-bad", "wrong.pdf", "text/plain", "invalid_pdf_mime", "mime"),
        (b"not-pdf", "wrong.pdf", "application/pdf", "invalid_pdf_signature", "signature"),
        (
            b"%PDF-not-a-structure",
            "broken.pdf",
            "application/pdf",
            "invalid_pdf_structure",
            "structure",
        ),
    ],
)
async def test_pdf_validation_rejections_leave_no_temporary_file(
    tmp_path: Path,
    content: bytes,
    filename: str,
    content_type: str,
    code: str,
    stage: str,
) -> None:
    storage = StorageService(tmp_path, max_pdf_bytes=1024 * 1024)

    with pytest.raises(UploadRejected) as captured:
        await storage.receive_and_validate(
            upload_file(content, filename=filename, content_type=content_type)
        )

    assert captured.value.code == code
    assert captured.value.stage == stage
    assert list(storage.temporary_root.iterdir()) == []


@pytest.mark.anyio
async def test_size_limit_stops_receive_and_removes_partial_file(tmp_path: Path) -> None:
    storage = StorageService(tmp_path, max_pdf_bytes=8)

    with pytest.raises(UploadRejected) as captured:
        await storage.receive_and_validate(upload_file(b"x" * 9))

    assert captured.value.code == "pdf_too_large"
    assert captured.value.status_code == 413
    assert list(storage.temporary_root.iterdir()) == []


@pytest.mark.anyio
async def test_interrupted_receive_removes_partial_file(tmp_path: Path) -> None:
    storage = StorageService(tmp_path, max_pdf_bytes=1024)

    with pytest.raises(ConnectionError):
        await storage.receive_and_validate(InterruptedUpload())

    assert list(storage.temporary_root.iterdir()) == []
    assert list(storage.files_root.rglob("*.pdf")) == []


@pytest.mark.anyio
async def test_publish_is_atomic_and_never_overwrites_existing_target(tmp_path: Path) -> None:
    storage = StorageService(tmp_path, max_pdf_bytes=1024 * 1024)
    first = await storage.receive_and_validate(upload_file(synthetic_pdf()))
    published = await storage.publish(first)
    before_bytes = published.absolute_path.read_bytes()
    before_mtime = published.absolute_path.stat().st_mtime_ns

    second = await storage.receive_and_validate(
        upload_file(synthetic_pdf(), filename="another.pdf")
    )
    reused = await storage.publish(second)

    assert published.moved
    assert not reused.moved
    assert reused.storage_path == published.storage_path
    assert reused.absolute_path.read_bytes() == before_bytes
    assert reused.absolute_path.stat().st_mtime_ns == before_mtime
    assert not second.temporary_path.exists()


def test_orphan_report_is_read_only_and_path_resolution_is_bounded(tmp_path: Path) -> None:
    storage = StorageService(tmp_path, max_pdf_bytes=1024)
    storage.prepare()
    digest = "a" * 64
    relative = storage.relative_path_for_hash(digest)
    path = storage.files_root / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")
    before = path.stat()

    orphans = storage.find_orphans(set())

    assert [orphan.storage_path for orphan in orphans] == [relative]
    assert path.read_bytes() == b"synthetic"
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert storage.find_orphans({relative}) == []
    assert storage.resolve_formal_path(relative) == path.resolve()
    with pytest.raises(RuntimeError):
        storage.resolve_formal_path("../../outside.pdf")
    with pytest.raises(ValueError):
        storage.relative_path_for_hash("../unsafe")
