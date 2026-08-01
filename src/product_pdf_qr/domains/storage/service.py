"""Isolated PDF validation and content-addressed storage."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from pypdf import PdfReader

from product_pdf_qr.errors import AppError

logger = logging.getLogger(__name__)

PDF_MIME_TYPE = "application/pdf"
PDF_MAGIC = b"%PDF-"
STREAM_CHUNK_BYTES = 1024 * 1024
STORAGE_PATH_PATTERN = re.compile(
    r"^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.pdf$",
    re.ASCII,
)


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


@dataclass(frozen=True, slots=True)
class OrphanFile:
    """One formal PDF which has no corresponding database reference."""

    storage_path: str
    size_bytes: int
    modified_at_ns: int


class StorageService:
    """Own the temporary and formal directories on one filesystem."""

    def __init__(self, root: Path, max_pdf_bytes: int) -> None:
        self.root = root
        self.temporary_root = root / "temporary"
        self.files_root = root / "files"
        self.max_pdf_bytes = max_pdf_bytes
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
        temporary_path = Path(temporary_name)
        size_bytes = 0
        digest = hashlib.sha256()
        output = os.fdopen(file_descriptor, "wb")
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
        except BaseException:
            output.close()
            temporary_path.unlink(missing_ok=True)
            raise
        finally:
            if not output.closed:
                output.close()

        try:
            self._validate_metadata(original_filename, upload.content_type)
            await asyncio.to_thread(self._validate_pdf_content, temporary_path, size_bytes)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

        return ValidatedUpload(
            temporary_path=temporary_path,
            original_filename=original_filename,
            size_bytes=size_bytes,
            sha256=digest.hexdigest(),
        )

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

    @staticmethod
    def _validate_pdf_content(path: Path, size_bytes: int) -> None:
        if size_bytes == 0:
            raise UploadRejected("empty_pdf", "PDF 文件不能为空。", "size")
        with path.open("rb") as stream:
            if stream.read(len(PDF_MAGIC)) != PDF_MAGIC:
                raise UploadRejected(
                    "invalid_pdf_signature",
                    "文件内容不是有效的 PDF。",
                    "signature",
                )
        try:
            reader = PdfReader(path, strict=True)
            len(reader.pages)
        except Exception as error:
            raise UploadRejected(
                "invalid_pdf_structure",
                "PDF 结构无法解析。",
                "structure",
            ) from error

    async def publish(self, upload: ValidatedUpload) -> PublishedFile:
        """Atomically rename one file into storage without overwriting existing content."""

        lock = await self._publish_lock(upload.sha256)
        async with lock:
            relative = self.relative_path_for_hash(upload.sha256)
            target = self.files_root / relative
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            if target.exists():
                upload.discard()
                return PublishedFile(relative, target, moved=False)
            await asyncio.to_thread(os.rename, upload.temporary_path, target)
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
