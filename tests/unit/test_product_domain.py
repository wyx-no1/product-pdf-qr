"""Product-code and public-token contract tests."""

from __future__ import annotations

import base64

import pytest

from product_pdf_qr.domains.product import (
    generate_public_token,
    is_normalized_product_code,
    normalize_product_code,
)
from product_pdf_qr.errors import AppError


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        (" a001_1 ", "A001_1"),
        ("a", "A"),
        ("Z" * 64, "Z" * 64),
        ("abc-123_DEF", "ABC-123_DEF"),
    ],
)
def test_normalize_product_code_accepts_contract_boundaries(
    raw: str,
    normalized: str,
) -> None:
    assert normalize_product_code(raw) == normalized
    assert is_normalized_product_code(normalized)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "中",
        "A 1",
        "A/B",
        "A.B",
        "A#B",
        "A%B",
        "A" * 65,
        "é",
    ],
)
def test_normalize_product_code_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(AppError) as captured:
        normalize_product_code(raw)

    assert captured.value.code == "invalid_product_code"
    assert captured.value.status_code == 422


def test_normalized_contract_rejects_lowercase_and_whitespace() -> None:
    assert not is_normalized_product_code("a001")
    assert not is_normalized_product_code(" A001")


def test_public_token_uses_16_csprng_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def fake_token_bytes(length: int) -> bytes:
        calls.append(length)
        return bytes(range(length))

    monkeypatch.setattr(
        "product_pdf_qr.domains.product.service.secrets.token_bytes",
        fake_token_bytes,
    )

    token = generate_public_token()

    assert calls == [16]
    assert len(token) == 26
    assert token == base64.b32encode(bytes(range(16))).decode("ascii").rstrip("=")
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def test_public_tokens_are_not_constant() -> None:
    tokens = {generate_public_token() for _ in range(256)}

    assert len(tokens) == 256
    assert all(len(token) == 26 for token in tokens)
