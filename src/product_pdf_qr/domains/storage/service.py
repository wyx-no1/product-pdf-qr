"""Isolated PDF validation and content-addressed storage."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import multiprocessing
import os
import re
import resource

# A fixed /bin/ps invocation reads this worker's own Darwin virtual size.
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Literal

from fastapi import UploadFile
from pypdf import PdfReader

from product_pdf_qr.config import (
    DEFAULT_PDF_VALIDATION_CPU_SECONDS,
    DEFAULT_PDF_VALIDATION_MEMORY_BYTES,
    DEFAULT_PDF_VALIDATION_TIMEOUT_SECONDS,
)
from product_pdf_qr.errors import AppError

logger = logging.getLogger(__name__)

PDF_MIME_TYPE = "application/pdf"
PDF_MAGIC = b"%PDF-"
STREAM_CHUNK_BYTES = 1024 * 1024
PDF_VALIDATION_POLL_SECONDS = 0.01
STORAGE_PATH_PATTERN = re.compile(
    r"^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.pdf$",
    re.ASCII,
)
PDFValidationStatus = Literal["valid", "invalid", "resource_limit"]
PDFParser = Callable[[str], PDFValidationStatus]


def _set_pdf_worker_limits(cpu_seconds: int, memory_bytes: int) -> None:
    """Apply hard process ceilings before parsing untrusted PDF structures."""

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    if sys.platform == "darwin":
        # dyld reserves hundreds of GiB of sparse virtual address space before
        # this worker starts. Limit additional address-space growth to the same
        # configured budget used as the absolute RLIMIT_AS ceiling on Linux.
        current_vsize_bytes = (
            int(
                subprocess.check_output(  # nosec B603
                    ["/bin/ps", "-o", "vsz=", "-p", str(os.getpid())],
                    text=True,
                ).strip()
            )
            * 1024
        )
        address_space_limit = current_vsize_bytes + memory_bytes
        resource.setrlimit(
            resource.RLIMIT_AS,
            (address_space_limit, address_space_limit),
        )
        return
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


def _parse_pdf(path: str) -> PDFValidationStatus:
    """Parse one existing file without network, database, or write access."""

    try:
        reader = PdfReader(path, strict=True)
        len(reader.pages)
    except MemoryError:
        return "resource_limit"
    except Exception:
        return "invalid"
    return "valid"


def _pdf_validation_worker(
    path: str,
    result_connection: Connection,
    cpu_seconds: int,
    memory_bytes: int,
    parser: PDFParser,
) -> None:
    """Apply resource ceilings, run the parser, and report a bounded status."""

    status: PDFValidationStatus
    try:
        _set_pdf_worker_limits(cpu_seconds, memory_bytes)
        status = parser(path)
    except MemoryError:
        status = "resource_limit"
    except (OSError, ValueError):
        status = "resource_limit"
    except Exception:
        status = "invalid"
    try:
        result_connection.send(status)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        result_connection.close()


class UploadRejected(AppError):
    """A safe upload rejection tagged with its validation stage."""

    def __init__(self, code: str, message: str, stage: str, status_code: int = 422) -> None:
        super().__init__(code, message, status_code)
        self.stage = stage


@dataclass(slots=True)
class ValidatedUpload:
    """A validated temporary file which has not entered formal storage."""

    temporary_path: Path
    original_filename: str
    size_bytes: int
    sha256: str

    def discard(self) -> None:
        """Remove the temporary file if it is still present."""

        try:
            self.temporary_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Temporary upload cleanup failed")


@dataclass(frozen=True, slots=True)
class PublishedFile:
    """The formal content-addressed location of a validated PDF."""

    storage_path: str
    absolute_path: Path
    moved: bool


class PublishCancelled(asyncio.CancelledError):
    """Cancellation observed after the atomic publication outcome was known."""

    def __init__(self, published: PublishedFile) -> None:
        super().__init__()
        self.published = published


@dataclass(frozen=True, slots=True)
class OrphanFile:
    """One formal PDF which has no corresponding database reference."""

    storage_path: str
    size_bytes: int
    modified_at_ns: int


class StorageService:
    """Own the temporary and formal directories on one filesystem."""

    def __init__(
        self,
        root: Path,
        max_pdf_bytes: int,
        *,
        pdf_validation_timeout_seconds: float = DEFAULT_PDF_VALIDATION_TIMEOUT_SECONDS,
        pdf_validation_cpu_seconds: int = DEFAULT_PDF_VALIDATION_CPU_SECONDS,
        pdf_validation_memory_bytes: int = DEFAULT_PDF_VALIDATION_MEMORY_BYTES,
        pdf_parser: PDFParser = _parse_pdf,
    ) -> None:
        self.root = root
        self.temporary_root = root / "temporary"
        self.files_root = root / "files"
        self.max_pdf_bytes = max_pdf_bytes
        self.pdf_validation_timeout_seconds = pdf_validation_timeout_seconds
        self.pdf_validation_cpu_seconds = pdf_validation_cpu_seconds
        self.pdf_validation_memory_bytes = pdf_validation_memory_bytes
        self.pdf_parser = pdf_parser
        self._publish_locks: dict[str, asyncio.Lock] = {}
        self._publish_locks_guard = asyncio.Lock()

    def prepare(self) -> None:
        """Create both directories below one configured storage root."""

        self.temporary_root.mkdir(parents=True, exist_ok=True)
        self.files_root.mkdir(parents=True, exist_ok=True)
        if self.temporary_root.stat().st_dev != self.files_root.stat().st_dev:
            raise RuntimeError("Temporary and formal storage must share one filesystem")

    async def receive_and_validate(self, upload: UploadFile) -> ValidatedUpload:
        """Stream to isolation, enforce limits, then validate content and hash it."""

        self.prepare()
        original_filename = upload.filename or ""
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix="upload-",
            suffix=".part",
            dir=self.temporary_root,
        )
        # Keep untrusted/incomplete input private. The group-class ACL mask is
        # restored only immediately before the validated file is published.
        temporary_path = Path(temporary_name)
        size_bytes = 0
        digest = hashlib.sha256()
        output = os.fdopen(file_descriptor, "wb")
        validation_succeeded = False
        try:
            while True:
                chunk = await upload.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > self.max_pdf_bytes:
                    raise UploadRejected(
                        "pdf_too_large",
                        "PDF 文件不得超过 50 MB。",
                        "size",
                        413,
                    )
                digest.update(chunk)
                await asyncio.to_thread(output.write, chunk)
            await asyncio.to_thread(output.flush)
            await asyncio.to_thread(os.fsync, output.fileno())
            output.close()
            self._validate_metadata(original_filename, upload.content_type)
            await self._validate_pdf_content(temporary_path, size_bytes)
            validation_succeeded = True
            return ValidatedUpload(
                temporary_path=temporary_path,
                original_filename=original_filename,
                size_bytes=size_bytes,
                sha256=digest.hexdigest(),
            )
        finally:
            if not output.closed:
                output.close()
            if not validation_succeeded:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _validate_metadata(filename: str, content_type: str | None) -> None:
        if Path(filename).suffix.lower() != ".pdf":
            raise UploadRejected(
                "invalid_pdf_extension",
                "仅允许上传 .pdf 文件。",
                "extension",
            )
        declared_type = (content_type or "").split(";", 1)[0].strip().lower()
        if declared_type != PDF_MIME_TYPE:
            raise UploadRejected(
                "invalid_pdf_mime",
                "文件声明类型必须为 application/pdf。",
                "mime",
            )

    async def _validate_pdf_content(self, path: Path, size_bytes: int) -> None:
        if size_bytes == 0:
            raise UploadRejected("empty_pdf", "PDF 文件不能为空。", "size")
        with path.open("rb") as stream:
            if stream.read(len(PDF_MAGIC)) != PDF_MAGIC:
                raise UploadRejected(
                    "invalid_pdf_signature",
                    "文件内容不是有效的 PDF。",
                    "signature",
                )
        context = multiprocessing.get_context("spawn")
        result_reader, result_writer = context.Pipe(duplex=False)
        process = context.Process(
            target=_pdf_validation_worker,
            args=(
                str(path),
                result_writer,
                self.pdf_validation_cpu_seconds,
                self.pdf_validation_memory_bytes,
                self.pdf_parser,
            ),
            daemon=True,
        )
        process_started = False
        timed_out = False
        try:
            process.start()
            process_started = True
            result_writer.close()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.pdf_validation_timeout_seconds
            while process.is_alive():
                if loop.time() >= deadline:
                    timed_out = True
                    process.kill()
                    break
                await asyncio.sleep(PDF_VALIDATION_POLL_SECONDS)
            await asyncio.to_thread(process.join)
            if timed_out:
                raise UploadRejected(
                    "pdf_validation_timeout",
                    "PDF 结构校验超时。",
                    "structure_timeout",
                )
            status: PDFValidationStatus | None = None
            if result_reader.poll():
                try:
                    status = result_reader.recv()
                except EOFError:
                    status = None
            if status == "valid":
                return
            if status == "resource_limit" or (
                status is None and process.exitcode is not None and process.exitcode < 0
            ):
                raise UploadRejected(
                    "pdf_validation_resource_limit",
                    "PDF 结构校验超过资源限制。",
                    "structure_resource",
                )
            raise UploadRejected(
                "invalid_pdf_structure",
                "PDF 结构无法解析。",
                "structure",
            )
        finally:
            if process_started and process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join)
            elif process_started and process.exitcode is None:
                await asyncio.to_thread(process.join)
            result_reader.close()
            result_writer.close()

    async def publish(self, upload: ValidatedUpload) -> PublishedFile:
        """Atomically rename one file into storage without overwriting existing content."""

        lock = await self._publish_lock(upload.sha256)
        async with lock:
            publication = asyncio.create_task(asyncio.to_thread(self._publish_sync, upload))
            try:
                return await asyncio.shield(publication)
            except asyncio.CancelledError as error:
                try:
                    published = await publication
                except Exception:
                    raise error from None
                raise PublishCancelled(published) from error

    def _publish_sync(self, upload: ValidatedUpload) -> PublishedFile:
        """Complete one indivisible publication attempt in a worker thread."""

        relative = self.relative_path_for_hash(upload.sha256)
        target = self.files_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            upload.discard()
            return PublishedFile(relative, target, moved=False)
        descriptor = os.open(
            upload.temporary_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fchmod(descriptor, 0o660)
            os.rename(upload.temporary_path, target)
        finally:
            os.close(descriptor)
        return PublishedFile(relative, target, moved=True)

    async def _publish_lock(self, sha256: str) -> asyncio.Lock:
        async with self._publish_locks_guard:
            return self._publish_locks.setdefault(sha256, asyncio.Lock())

    @staticmethod
    def relative_path_for_hash(sha256: str) -> str:
        """Derive a path using only a validated SHA-256 digest."""

        if re.fullmatch(r"[0-9a-f]{64}", sha256, re.ASCII) is None:
            raise ValueError("Invalid SHA-256 digest")
        return f"{sha256[:2]}/{sha256[2:4]}/{sha256}.pdf"

    def resolve_formal_path(self, storage_path: str) -> Path:
        """Resolve a database path and prove that it stays under the file root."""

        if STORAGE_PATH_PATTERN.fullmatch(storage_path) is None:
            raise RuntimeError("Unsafe storage path")
        root = self.files_root.resolve()
        resolved = (root / storage_path).resolve()
        if not resolved.is_relative_to(root):
            raise RuntimeError("Storage path escaped the configured root")
        return resolved

    def find_orphans(self, referenced_paths: set[str]) -> list[OrphanFile]:
        """Return a read-only snapshot of unreferenced formal files."""

        self.prepare()
        orphans: list[OrphanFile] = []
        for path in sorted(self.files_root.rglob("*.pdf")):
            relative = path.relative_to(self.files_root).as_posix()
            if relative in referenced_paths:
                continue
            stat = path.stat()
            orphans.append(
                OrphanFile(
                    storage_path=relative,
                    size_bytes=stat.st_size,
                    modified_at_ns=stat.st_mtime_ns,
                )
            )
        return orphans
