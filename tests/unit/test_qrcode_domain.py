"""QR specification, determinism, cache, and failure-compensation tests."""

from __future__ import annotations

import io
import os
import stat
import struct
from pathlib import Path

import pytest
import segno
from PIL import Image, ImageDraw

from product_pdf_qr.domains.qrcode import QRCodeGenerationError, QRCodeService

TOKEN = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def png_chunks(image_bytes: bytes) -> list[str]:
    position = 8
    chunks: list[str] = []
    while position < len(image_bytes):
        length = struct.unpack(">I", image_bytes[position : position + 4])[0]
        chunks.append(image_bytes[position + 4 : position + 8].decode("ascii"))
        position += length + 12
    return chunks


def test_qrcode_is_deterministic_labelled_png_without_metadata(tmp_path: Path) -> None:
    service = QRCodeService(tmp_path, "http://127.0.0.1:8000/")

    first = service.generate("A001", TOKEN, "测试产品")
    second = service.generate("A001", TOKEN, "测试产品")

    assert first == second
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    image = Image.open(io.BytesIO(first))
    assert image.size == (1024, 1280)
    assert image.info == {}
    assert png_chunks(first) == ["IHDR", "IDAT", "IEND"]
    assert service.public_url(TOKEN) == f"http://127.0.0.1:8000/p/{TOKEN}"
    assert b"A001" not in first


def test_rendered_modules_match_h_level_utf8_public_url(tmp_path: Path) -> None:
    service = QRCodeService(tmp_path, "http://127.0.0.1:8000")
    payload = service.public_url(TOKEN)
    image = Image.open(io.BytesIO(service.generate("A001", TOKEN, "测试产品")))
    symbol = segno.make(
        payload,
        error="h",
        encoding="utf-8",
        micro=False,
        boost_error=False,
    )
    matrix = tuple(tuple(bool(module) for module in row) for row in symbol.matrix)
    module_count = len(matrix) + 8
    pixels = image.load()
    assert pixels is not None

    for row_index, row in enumerate(matrix):
        for column_index, expected_dark in enumerate(row):
            x = ((column_index + 4) * 1024 + 512) // module_count
            y = ((row_index + 4) * 1024 + 512) // module_count
            assert (pixels[x, y] == 0) is expected_dark
    for border_index in range(4):
        coordinate = (border_index * 1024 + 512) // module_count
        assert pixels[coordinate, coordinate] != 0


@pytest.mark.anyio
async def test_cache_miss_generates_and_next_request_hits(tmp_path: Path) -> None:
    service = QRCodeService(tmp_path, "http://127.0.0.1:8000")

    first = await service.get_or_generate("A001", TOKEN, "测试产品")
    second = await service.get_or_generate("A001", TOKEN, "测试产品")

    assert not first.cache_hit
    assert first.cache_error is None
    assert second.cache_hit
    assert second.image_bytes == first.image_bytes
    cache_path = service.cache_root / "A001.png"
    assert cache_path.read_bytes() == first.image_bytes
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o660
    assert stat.S_IMODE(cache_path.stat().st_mode) & 0o007 == 0


@pytest.mark.anyio
async def test_cache_acl_mask_failure_leaves_no_file_or_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = QRCodeService(tmp_path, "http://127.0.0.1:8000")

    def fail_fchmod(_descriptor: int, _mode: int) -> None:
        raise OSError("synthetic ACL mask failure")

    monkeypatch.setattr(os, "fchmod", fail_fchmod)

    result = await service.get_or_generate("A001", TOKEN, "测试产品")

    assert result.cache_error == "OSError"
    assert not (service.cache_root / "A001.png").exists()
    assert list(service.cache_root.glob(".*.tmp")) == []


@pytest.mark.anyio
async def test_cache_write_failure_returns_memory_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = QRCodeService(tmp_path, "http://127.0.0.1:8000")

    def fail_cache(_target: Path, _image_bytes: bytes) -> None:
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(service, "_write_cache_atomically", fail_cache)

    result = await service.get_or_generate("A001", TOKEN, "测试产品")

    assert result.image_bytes.startswith(b"\x89PNG")
    assert not result.cache_hit
    assert result.cache_error == "OSError"
    assert not (service.cache_root / "A001.png").exists()


def test_invalid_stored_code_never_generates_placeholder(tmp_path: Path) -> None:
    service = QRCodeService(tmp_path, "http://127.0.0.1:8000")

    with pytest.raises(QRCodeGenerationError):
        service.generate("../A001", TOKEN, "测试产品")

    assert list(tmp_path.rglob("*.png")) == []


def test_product_name_is_rendered_below_unchanged_qr_symbol(tmp_path: Path) -> None:
    service = QRCodeService(tmp_path, "http://127.0.0.1:8000")

    first = Image.open(io.BytesIO(service.generate("A001", TOKEN, "透明离心管")))
    second = Image.open(io.BytesIO(service.generate("A001", TOKEN, "高速离心机")))

    assert first.crop((0, 0, 1024, 1024)).tobytes() == second.crop((0, 0, 1024, 1024)).tobytes()
    assert (
        first.crop((0, 1024, 1024, 1280)).tobytes() != second.crop((0, 1024, 1024, 1280)).tobytes()
    )
    assert first.crop((0, 1024, 1024, 1280)).getextrema() == (0, 255)


def test_maximum_length_product_name_wraps_without_truncation(tmp_path: Path) -> None:
    service = QRCodeService(tmp_path, "http://127.0.0.1:8000")
    product_name = "超长中文产品名称" * 15
    canvas = Image.new("L", (1024, 1280), color=255)
    draw = ImageDraw.Draw(canvas)

    _font, lines, line_height = service._fit_product_name(draw, product_name)
    rendered = service.generate("A001", TOKEN, product_name)

    assert len(product_name) == 120
    assert "".join(lines) == product_name
    assert (line_height * len(lines)) + (8 * (len(lines) - 1)) <= 216
    assert Image.open(io.BytesIO(rendered)).size == (1024, 1280)


@pytest.mark.anyio
async def test_legacy_unlabelled_cache_is_not_reused(tmp_path: Path) -> None:
    legacy_cache = tmp_path / "qrcodes"
    legacy_cache.mkdir()
    legacy_image = Image.new("1", (1024, 1024), color=1)
    legacy_output = io.BytesIO()
    legacy_image.save(legacy_output, format="PNG")
    (legacy_cache / "A001.png").write_bytes(legacy_output.getvalue())
    service = QRCodeService(tmp_path, "http://127.0.0.1:8000")

    result = await service.get_or_generate("A001", TOKEN, "测试产品")

    assert not result.cache_hit
    assert result.image_bytes.startswith(b"\x89PNG")
    assert Image.open(service.cache_root / "A001.png").size == (1024, 1280)
