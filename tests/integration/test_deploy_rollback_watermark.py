"""Isolated PostgreSQL coverage for the complete PR2B watermark."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from scripts.deploy_rollback.model import validate_watermark
from scripts.deploy_rollback.watermark import RELATIONS, build_watermark

ROOT = Path(__file__).parents[2]


@pytest.mark.integration
def test_pr2b_watermark_uses_read_only_database_and_content_file_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_url = os.environ.get("TEST_MIGRATION_DATABASE_URL")
    backup_url = os.environ.get("TEST_BACKUP_DATABASE_URL")
    if not migration_url or not backup_url:
        pytest.fail(
            "TEST_MIGRATION_DATABASE_URL and TEST_BACKUP_DATABASE_URL are required "
            "for integration tests"
        )
    configuration = Config(str(ROOT / "alembic.ini"))
    monkeypatch.setenv("DATABASE_URL", migration_url)
    command.upgrade(configuration, "head")

    file_root = tmp_path / "files"
    file_root.mkdir()
    document = file_root / "history.pdf"
    document.write_bytes(b"%PDF-1.4\nsynthetic rollback watermark\n")

    observed = build_watermark(database_url=backup_url, file_root=file_root)

    assert set(observed["relations"]) == set(RELATIONS)
    assert observed["audit_projection"] == observed["relations"]["audit_events"]
    assert observed["files"] == [
        {
            "path": "history.pdf",
            "size": document.stat().st_size,
            "sha256": "88a377372b9b1ce97e26cd8a5bfc1250b8ba2ad831f960b27bdaa43cc41d2529",
        }
    ]
    validate_watermark(observed)
