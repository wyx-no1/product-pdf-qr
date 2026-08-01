"""Public-state ordering and missing-token limiter tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from product_pdf_qr.domains.public import PublicMissLimiter


@pytest.mark.anyio
async def test_miss_limiter_triggers_at_threshold_and_expires() -> None:
    now = 100.0

    def clock() -> float:
        return now

    limiter = PublicMissLimiter(limit=2, window_seconds=10, clock=clock)

    assert not await limiter.is_limited("client")
    assert not await limiter.register_miss("client")
    assert await limiter.register_miss("client")
    assert await limiter.is_limited("client")

    now = 111.0
    assert not await limiter.is_limited("client")


@pytest.mark.anyio
async def test_valid_activity_does_not_enter_miss_limiter() -> None:
    limiter = PublicMissLimiter(limit=1, window_seconds=600)

    for _ in range(1_000):
        assert not await limiter.is_limited("shared-ip")


def test_repository_has_no_orphan_cleanup_implementation() -> None:
    source_root = Path(__file__).parents[2] / "src" / "product_pdf_qr"
    source = "\n".join(path.read_text() for path in source_root.rglob("*.py"))

    assert "delete_orphan" not in source
    assert "cleanup_orphan" not in source
