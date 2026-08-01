"""Deterministic QR generation and best-effort derived caching."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import segno
from PIL import Image, ImageDraw

from product_pdf_qr.domains.product import is_normalized_product_code
from product_pdf_qr.errors import AppError

logger = logging.getLogger(__name__)

QR_IMAGE_SIZE = 1024
QR_BORDER_MODULES = 4


class QRCodeGenerationError(AppError):
    """A QR image cannot be safely generated for the requested product."""

    def __init__(self, message: str = "二维码生成失败。") -> None:
        super().__init__("qrcode_generation_failed", message, 422)


@dataclass(frozen=True, slots=True)
class QRCodeResult:
    """A QR response, including non-fatal cache write information."""

    image_bytes: bytes
    cache_hit: bool
    cache_error: str | None = None


class QRCodeService:
    """Generate 1024px H-level PNGs and cache them outside business transactions."""

    def __init__(self, storage_root: Path, public_base_url: str) -> None:
        self.cache_root = storage_root / "qrcodes"
        self.public_base_url = public_base_url.rstrip("/")
        self._cache_locks: dict[str, asyncio.Lock] = {}
        self._cache_locks_guard = asyncio.Lock()

    def public_url(self, public_token: str) -> str:
        """Build the only payload permitted in a QR code."""

        return f"{self.public_base_url}/p/{public_token}"

    def generate(self, code: str, public_token: str) -> bytes:
        """Generate deterministic PNG bytes with no ancillary metadata."""

        if not is_normalized_product_code(code):
            raise QRCodeGenerationError("产品编码不符合二维码文件名契约。")
        payload = self.public_url(public_token)
        try:
            symbol = segno.make(
                payload,
                error="h",
                encoding="utf-8",
                micro=False,
                boost_error=False,
            )
            matrix = tuple(tuple(bool(module) for module in row) for row in symbol.matrix)
            if not matrix or any(len(row) != len(matrix) for row in matrix):
                raise RuntimeError("QR matrix is not square")
            module_count = len(matrix) + (2 * QR_BORDER_MODULES)
            image = Image.new("1", (QR_IMAGE_SIZE, QR_IMAGE_SIZE), color=1)
            draw = ImageDraw.Draw(image)
            for row_index, row in enumerate(matrix):
                for column_index, is_dark in enumerate(row):
                    if not is_dark:
                        continue
                    x0 = ((column_index + QR_BORDER_MODULES) * QR_IMAGE_SIZE) // module_count
                    y0 = ((row_index + QR_BORDER_MODULES) * QR_IMAGE_SIZE) // module_count
                    x1 = (
                        ((column_index + QR_BORDER_MODULES + 1) * QR_IMAGE_SIZE) // module_count
                    ) - 1
                    y1 = (((row_index + QR_BORDER_MODULES + 1) * QR_IMAGE_SIZE) // module_count) - 1
                    draw.rectangle((x0, y0, x1, y1), fill=0)
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=False, compress_level=9)
            return output.getvalue()
        except QRCodeGenerationError:
            raise
        except Exception as error:
            raise QRCodeGenerationError() from error

    async def get_or_generate(self, code: str, public_token: str) -> QRCodeResult:
        """Return cached bytes or generate immediately and attempt an atomic cache write."""

        cache_path = self.cache_root / f"{code}.png"
        try:
            cached = await asyncio.to_thread(cache_path.read_bytes)
        except FileNotFoundError:
            cached = None
        except OSError:
            cached = None
        if cached is not None:
            return QRCodeResult(cached, cache_hit=True)

        image_bytes = self.generate(code, public_token)
        lock = await self._cache_lock(code)
        async with lock:
            try:
                cached = await asyncio.to_thread(cache_path.read_bytes)
            except (FileNotFoundError, OSError):
                cached = None
            if cached is not None:
                return QRCodeResult(cached, cache_hit=True)
            try:
                await asyncio.to_thread(
                    self._write_cache_without_overwrite,
                    cache_path,
                    image_bytes,
                )
            except OSError as error:
                logger.warning("QR cache write failed", extra={"product_code": code})
                return QRCodeResult(
                    image_bytes,
                    cache_hit=False,
                    cache_error=type(error).__name__,
                )
        return QRCodeResult(image_bytes, cache_hit=False)

    async def _cache_lock(self, code: str) -> asyncio.Lock:
        async with self._cache_locks_guard:
            return self._cache_locks.setdefault(code, asyncio.Lock())

    def _write_cache_without_overwrite(self, target: Path, image_bytes: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.stem}-",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as output:
                output.write(image_bytes)
                output.flush()
                os.fsync(output.fileno())
            if target.exists():
                temporary_path.unlink(missing_ok=True)
                return
            os.rename(temporary_path, target)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
