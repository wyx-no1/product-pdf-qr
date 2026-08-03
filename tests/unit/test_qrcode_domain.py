"""QR specification, determinism, cache, and failure-compensation tests."""

from __future__ import annotations

import io
import struct
from pathlib import Path

import pytest
import segno
from PIL import Image

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


def test_qrcode_is_deterministic_1024_png_without_metadata(tmp_path: Path) -> None:
    service = QRCodeService(tmp_path, "http://127.0.0.1:8000/")

    first = service.generate("A001", TOKEN)
    second = service.generate("A001", TOKEN)

    assert first == second
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    image = Image.open(io.BytesIO(first))
    assert image.size == (1024, 1024)
    assert image.info == {}
    assert png_chunks(first) == ["IHDR", "IDAT", "IEND"]
    assert service.public_url(TOKEN) == f"http://127.0.0.1:8000/p/{TOKEN}"
    assert b"A001" not in first


def test_rendered_modules_match_h_level_utf8_public_url(tmp_path: Path) -> None:
    service = QRCodeService(tmp_path, "http://127.0.0.1:8000")
    payload = service.public_url(TOKEN)
    image = Image.open(io.BytesIO(service.generate("A001", TOKEN)))
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

    first = await service.get_or_generate("A001", TOKEN)
    second = await service.get_or_generate("A001", TOKEN)

    assert not first.cache_hit
    assert first.cache_error is None
    assert second.cache_hit
    assert second.image_bytes == first.image_bytes
    assert (service.cache_root / "A001.png").read_bytes() == first.image_bytes


@pytest.mark.anyio
async def test_cache_write_failure_returns_memory_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = QRCodeService(tmp_path, "http://127.0.0.1:8000")

    def fail_cache(_target: Path, _image_bytes: bytes) -> None:
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(service, "_write_cache_without_overwrite", fail_cache)

    result = await service.get_or_generate("A001", TOKEN)

    assert result.image_bytes.startswith(b"\x89PNG")
    assert not result.cache_hit
    assert result.cache_error == "OSError"
    assert not (service.cache_root / "A001.png").exists()


def test_invalid_stored_code_never_generates_placeholder(tmp_path: Path) -> None:
    service = QRCodeService(tmp_path, "http://127.0.0.1:8000")

    with pytest.raises(QRCodeGenerationError):
        service.generate("../A001", TOKEN)

    assert list(tmp_path.rglob("*.png")) == []
