"""End-to-end synthetic execution of the shipped G-19 shell orchestration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.deploy_rollback.rehearsal_e2e import run_round


@pytest.mark.integration
def test_real_pr2b_wrappers_and_pr2a_restore_preserve_required_data_semantics(
    tmp_path: Path,
) -> None:
    migration_url = os.environ.get("TEST_MIGRATION_DATABASE_URL")
    backup_url = os.environ.get("TEST_BACKUP_DATABASE_URL")
    if not migration_url or not backup_url:
        pytest.fail(
            "TEST_MIGRATION_DATABASE_URL and TEST_BACKUP_DATABASE_URL are required "
            "for orchestration integration tests"
        )
    run_round(tmp_path, migration_url, backup_url)
