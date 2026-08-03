from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ao.git import GitRepository
from scripts.ao.trust import (
    TRUSTED_CI_WORKFLOW_PATH,
    CIWorkflowTrustError,
    verify_ci_workflow_definition,
)
from tests.ao.helpers import commit_paths, git

TRUSTED_CI = """name: CI
on:
  pull_request:
jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - run: make test-unit
  database:
    runs-on: ubuntu-latest
    steps:
      - run: make test-integration
  container:
    runs-on: ubuntu-latest
    steps:
      - run: docker compose build
"""

NO_OP_CI = """name: CI
on:
  pull_request:
jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - run: "true"
  database:
    runs-on: ubuntu-latest
    steps:
      - run: "true"
  container:
    runs-on: ubuntu-latest
    steps:
      - run: "true"
"""


def test_candidate_unchanged_ci_definition_is_trusted(tmp_path: Path) -> None:
    trusted, candidate = _repositories(tmp_path)
    (candidate / "feature.txt").write_text("candidate code\n", encoding="utf-8")
    candidate_sha = commit_paths(candidate, "feat: candidate code", ["feature.txt"])

    verified = verify_ci_workflow_definition(
        GitRepository(trusted),
        GitRepository(candidate),
        candidate_sha,
        TRUSTED_CI_WORKFLOW_PATH,
    )

    assert verified.candidate_commit_sha == candidate_sha
    assert verified.path == TRUSTED_CI_WORKFLOW_PATH
    assert len(verified.blob_sha) == 40


def test_candidate_modified_no_op_ci_cannot_trigger_publication(tmp_path: Path) -> None:
    trusted, candidate = _repositories(tmp_path)
    workflow = candidate / TRUSTED_CI_WORKFLOW_PATH
    workflow.write_text(NO_OP_CI, encoding="utf-8")
    candidate_sha = commit_paths(
        candidate,
        "ci: replace gates with no-op jobs",
        [TRUSTED_CI_WORKFLOW_PATH],
    )

    with pytest.raises(CIWorkflowTrustError, match="tree entry differs"):
        verify_ci_workflow_definition(
            GitRepository(trusted),
            GitRepository(candidate),
            candidate_sha,
            TRUSTED_CI_WORKFLOW_PATH,
        )


def test_differently_named_candidate_workflow_cannot_spoof_ci_run(tmp_path: Path) -> None:
    trusted, candidate = _repositories(tmp_path)
    candidate_sha = git(candidate, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(CIWorkflowTrustError, match="source CI run path"):
        verify_ci_workflow_definition(
            GitRepository(trusted),
            GitRepository(candidate),
            candidate_sha,
            ".github/workflows/no-op-ci.yml",
        )


def _repositories(tmp_path: Path) -> tuple[Path, Path]:
    trusted = tmp_path / "trusted"
    candidate = tmp_path / "candidate"
    git(tmp_path, "init", "-b", "main", str(trusted))
    git(trusted, "config", "user.name", "AO Test")
    git(trusted, "config", "user.email", "ao@example.invalid")
    workflow = trusted / TRUSTED_CI_WORKFLOW_PATH
    workflow.parent.mkdir(parents=True)
    workflow.write_text(TRUSTED_CI, encoding="utf-8")
    commit_paths(trusted, "ci: trusted gates", [TRUSTED_CI_WORKFLOW_PATH])
    git(tmp_path, "clone", str(trusted), str(candidate))
    git(candidate, "config", "user.name", "AO Test")
    git(candidate, "config", "user.email", "ao@example.invalid")
    return trusted, candidate
