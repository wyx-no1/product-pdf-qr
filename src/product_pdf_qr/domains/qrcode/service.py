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
from PIL import Image, ImageDraw, ImageFont
from PIL.ImageFont import FreeTypeFont

from product_pdf_qr.domains.product import is_normalized_product_code
from product_pdf_qr.errors import AppError

logger = logging.getLogger(__name__)

QR_IMAGE_SIZE = 1024
QR_LABEL_HEIGHT = 256
QR_CANVAS_HEIGHT = QR_IMAGE_SIZE + QR_LABEL_HEIGHT
QR_BORDER_MODULES = 4
QR_LABEL_HORIZONTAL_PADDING = 40
QR_LABEL_VERTICAL_PADDING = 20
QR_LABEL_MAX_FONT_SIZE = 64
QR_LABEL_MIN_FONT_SIZE = 24
QR_LABEL_LINE_SPACING = 8
QR_FONT_PATH = Path(__file__).resolve().parents[2] / "assets" / "NotoSansCJKsc-Regular.otf"


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
    """Generate labelled H-level PNGs and cache them outside business transactions."""

    def __init__(self, storage_root: Path, public_base_url: str) -> None:
        self.cache_root = storage_root / "qrcodes"
        self.public_base_url = public_base_url.rstrip("/")
        self._cache_locks: dict[str, asyncio.Lock] = {}
        self._cache_locks_guard = asyncio.Lock()

    def public_url(self, public_token: str) -> str:
        """Build the only payload permitted in a QR code."""

        return f"{self.public_base_url}/p/{public_token}"

    def generate(self, code: str, public_token: str, product_name: str) -> bytes:
        """Generate deterministic QR-and-name PNG bytes with no metadata."""

        if not is_normalized_product_code(code):
            raise QRCodeGenerationError("产品编码不符合二维码文件名契约。")
        normalized_name = product_name.strip()
        if not normalized_name or len(normalized_name) > 120:
            raise QRCodeGenerationError("产品名称不符合二维码标签契约。")
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
            image = Image.new("L", (QR_IMAGE_SIZE, QR_CANVAS_HEIGHT), color=255)
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
            self._draw_product_name(draw, normalized_name)
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=False, compress_level=9)
            return output.getvalue()
        except QRCodeGenerationError:
            raise
        except Exception as error:
            raise QRCodeGenerationError() from error

    async def get_or_generate(
        self,
        code: str,
        public_token: str,
        product_name: str,
    ) -> QRCodeResult:
        """Return cached bytes or generate immediately and attempt an atomic cache write."""

        cache_path = self.cache_root / f"{code}.png"
        cached = await asyncio.to_thread(self._read_current_cache, cache_path)
        if cached is not None:
            return QRCodeResult(cached, cache_hit=True)

        image_bytes = self.generate(code, public_token, product_name)
        lock = await self._cache_lock(code)
        async with lock:
            cached = await asyncio.to_thread(self._read_current_cache, cache_path)
            if cached is not None:
                return QRCodeResult(cached, cache_hit=True)
            try:
                await asyncio.to_thread(
                    self._write_cache_atomically,
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

    def _draw_product_name(self, draw: ImageDraw.ImageDraw, product_name: str) -> None:
        font, lines, line_height = self._fit_product_name(draw, product_name)
        total_height = (line_height * len(lines)) + (QR_LABEL_LINE_SPACING * (len(lines) - 1))
        current_y = QR_IMAGE_SIZE + ((QR_LABEL_HEIGHT - total_height) // 2)
        for line in lines:
            left, top, right, _bottom = draw.textbbox((0, 0), line, font=font)
            text_width = right - left
            text_x = ((QR_IMAGE_SIZE - text_width) // 2) - left
            draw.text((text_x, current_y - top), line, font=font, fill=0)
            current_y += line_height + QR_LABEL_LINE_SPACING

    def _fit_product_name(
        self,
        draw: ImageDraw.ImageDraw,
        product_name: str,
    ) -> tuple[FreeTypeFont, tuple[str, ...], int]:
        maximum_width = QR_IMAGE_SIZE - (2 * QR_LABEL_HORIZONTAL_PADDING)
        maximum_height = QR_LABEL_HEIGHT - (2 * QR_LABEL_VERTICAL_PADDING)
        for font_size in range(
            QR_LABEL_MAX_FONT_SIZE,
            QR_LABEL_MIN_FONT_SIZE - 1,
            -2,
        ):
            font = ImageFont.truetype(str(QR_FONT_PATH), font_size)
            lines = self._wrap_product_name(draw, product_name, font, maximum_width)
            ascent, descent = font.getmetrics()
            line_height = ascent + descent
            total_height = (line_height * len(lines)) + (QR_LABEL_LINE_SPACING * (len(lines) - 1))
            if total_height <= maximum_height:
                return font, lines, line_height
        raise QRCodeGenerationError("产品名称无法完整放入二维码标签区域。")

    @staticmethod
    def _wrap_product_name(
        draw: ImageDraw.ImageDraw,
        product_name: str,
        font: FreeTypeFont,
        maximum_width: int,
    ) -> tuple[str, ...]:
        lines: list[str] = []
        current = ""
        for character in product_name:
            candidate = f"{current}{character}"
            if current and draw.textlength(candidate, font=font) > maximum_width:
                lines.append(current)
                current = character
            else:
                current = candidate
            if draw.textlength(current, font=font) > maximum_width:
                raise QRCodeGenerationError("产品名称包含无法放入标签区域的字符。")
        if current:
            lines.append(current)
        return tuple(lines)

    @staticmethod
    def _read_current_cache(cache_path: Path) -> bytes | None:
        try:
            image_bytes = cache_path.read_bytes()
            with Image.open(io.BytesIO(image_bytes)) as image:
                if image.format != "PNG" or image.size != (QR_IMAGE_SIZE, QR_CANVAS_HEIGHT):
                    return None
                image.load()
            return image_bytes
        except (FileNotFoundError, OSError, ValueError):
            return None

    async def _cache_lock(self, code: str) -> asyncio.Lock:
        async with self._cache_locks_guard:
            return self._cache_locks.setdefault(code, asyncio.Lock())

    def _write_cache_atomically(self, target: Path, image_bytes: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.stem}-",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            # mkstemp(0600) closes the inherited default ACL mask. Reopen only
            # the group-class mask so the volume's named UID-10002 ACL can read
            # newly generated cache files; unrelated users remain denied.
            try:
                os.fchmod(file_descriptor, 0o660)
            except BaseException:
                os.close(file_descriptor)
                raise
            with os.fdopen(file_descriptor, "wb") as output:
                output.write(image_bytes)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, target)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
