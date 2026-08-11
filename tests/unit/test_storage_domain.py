"""PDF isolation, validation, publication, and reconciliation tests."""

from __future__ import annotations

import io
import multiprocessing
import os
import resource
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

import pytest
from fastapi import UploadFile
from pypdf import PdfWriter
from starlette.datastructures import Headers

from product_pdf_qr.domains.storage import StorageService, UploadRejected
from product_pdf_qr.domains.storage.service import _parse_pdf, _set_pdf_worker_limits


def synthetic_pdf() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return output.getvalue()


def slow_pdf_parser(_path: str) -> Literal["valid", "invalid", "resource_limit"]:
    """Simulate a compact input that traps its structural parser."""

    time.sleep(30)
    return "valid"


def memory_exhausting_pdf_parser(
    _path: str,
) -> Literal["valid", "invalid", "resource_limit"]:
    """Simulate the parser's deterministic response to exhausted address space."""

    raise MemoryError


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


def test_pdf_parser_accepts_normal_and_rejects_damaged_structure(tmp_path: Path) -> None:
    normal = tmp_path / "normal.pdf"
    damaged = tmp_path / "damaged.pdf"
    normal.write_bytes(synthetic_pdf())
    damaged.write_bytes(b"%PDF-not-a-structure")

    assert _parse_pdf(str(normal)) == "valid"
    assert _parse_pdf(str(damaged)) == "invalid"


def test_worker_limits_set_cpu_and_linux_address_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda resource_id, values: limits.append((resource_id, values)),
    )

    _set_pdf_worker_limits(3, 512)

    assert limits == [
        (resource.RLIMIT_CPU, (3, 4)),
        (resource.RLIMIT_AS, (512, 512)),
    ]


def test_worker_limits_allow_only_configured_growth_on_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: "1000\n",
    )
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda resource_id, values: limits.append((resource_id, values)),
    )

    _set_pdf_worker_limits(3, 512)

    assert limits == [
        (resource.RLIMIT_CPU, (3, 4)),
        (
            resource.RLIMIT_AS,
            (1000 * 1024 + 512, 1000 * 1024 + 512),
        ),
    ]


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
    assert stat.S_IMODE(validated.temporary_path.stat().st_mode) == 0o600
    assert ".." not in storage.relative_path_for_hash(validated.sha256)
    validated.discard()
    assert not validated.temporary_path.exists()


@pytest.mark.anyio
async def test_shared_acl_mode_is_applied_only_at_formal_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = StorageService(tmp_path, max_pdf_bytes=1024 * 1024)

    def fail_fchmod(_descriptor: int, _mode: int) -> None:
        raise OSError("synthetic ACL-mask failure")

    validated = await storage.receive_and_validate(upload_file(synthetic_pdf()))
    assert stat.S_IMODE(validated.temporary_path.stat().st_mode) == 0o600
    monkeypatch.setattr(os, "fchmod", fail_fchmod)
    with pytest.raises(OSError, match="ACL-mask failure"):
        await storage.publish(validated)

    assert validated.temporary_path.is_file()
    assert stat.S_IMODE(validated.temporary_path.stat().st_mode) == 0o600
    validated.discard()


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
async def test_slow_pdf_parser_is_killed_and_reaped_at_wall_clock_limit(
    tmp_path: Path,
) -> None:
    children_before = {child.pid for child in multiprocessing.active_children()}
    storage = StorageService(
        tmp_path,
        max_pdf_bytes=1024 * 1024,
        pdf_validation_timeout_seconds=0.1,
        pdf_parser=slow_pdf_parser,
    )

    started = time.monotonic()
    with pytest.raises(UploadRejected) as captured:
        await storage.receive_and_validate(upload_file(synthetic_pdf()))
    elapsed = time.monotonic() - started

    assert captured.value.code == "pdf_validation_timeout"
    assert captured.value.stage == "structure_timeout"
    assert elapsed < 2
    assert {child.pid for child in multiprocessing.active_children()} == children_before
    assert list(storage.temporary_root.iterdir()) == []


@pytest.mark.anyio
async def test_pdf_parser_memory_exhaustion_is_a_distinct_rejection(tmp_path: Path) -> None:
    storage = StorageService(
        tmp_path,
        max_pdf_bytes=1024 * 1024,
        pdf_parser=memory_exhausting_pdf_parser,
    )

    with pytest.raises(UploadRejected) as captured:
        await storage.receive_and_validate(upload_file(synthetic_pdf()))

    assert captured.value.code == "pdf_validation_resource_limit"
    assert captured.value.stage == "structure_resource"
    assert list(storage.temporary_root.iterdir()) == []


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
