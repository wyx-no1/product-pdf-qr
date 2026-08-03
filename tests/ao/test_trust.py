from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ao.git import GitRepository
from scripts.ao.trust import (
    TRUSTED_CI_EXACT_PATHS,
    TRUSTED_CI_TREE_PATHS,
    TRUSTED_CI_WORKFLOW_PATH,
    CIDefinitionComparison,
    CIWorkflowTrustError,
    compare_ci_definition,
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

NO_OP_CI = (
    TRUSTED_CI.replace("make test-unit", "true")
    .replace(
        "make test-integration",
        "true",
    )
    .replace("docker compose build", "true")
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_matching_gate_definition_is_trusted(tmp_path: Path) -> None:
    trusted, candidate = _repositories(tmp_path)
    (candidate / "feature.txt").write_text("candidate code\n", encoding="utf-8")
    candidate_sha = commit_paths(candidate, "feat: candidate code", ["feature.txt"])

    comparison = _compare(trusted, candidate, candidate_sha)

    assert comparison.status == "trusted"
    assert comparison.trusted_definition_hash == comparison.candidate_definition_hash
    assert comparison.differing_paths == ()
    assert len(comparison.trusted_definition_hash) == 64


@pytest.mark.parametrize(
    ("path", "content"),
    [
        (TRUSTED_CI_WORKFLOW_PATH, NO_OP_CI),
        ("Makefile", "test-unit:\n\ttrue\n"),
        (
            "pyproject.toml",
            "[tool.coverage.report]\nfail_under = 0\n[tool.mypy]\nstrict = false\n",
        ),
    ],
)
def test_candidate_gate_definition_change_requires_re_review(
    tmp_path: Path,
    path: str,
    content: str,
) -> None:
    trusted, candidate = _repositories(tmp_path)
    (candidate / path).write_text(content, encoding="utf-8")
    candidate_sha = commit_paths(candidate, "ci: weaken trusted gate input", [path])

    comparison = _compare(trusted, candidate, candidate_sha)

    assert comparison.status == "requires-re-review"
    assert comparison.trusted_definition_hash != comparison.candidate_definition_hash
    assert path in comparison.differing_paths


def test_restored_gate_definition_becomes_trusted_again(tmp_path: Path) -> None:
    trusted, candidate = _repositories(tmp_path)
    workflow = candidate / TRUSTED_CI_WORKFLOW_PATH
    workflow.write_text(NO_OP_CI, encoding="utf-8")
    commit_paths(candidate, "ci: weaken gates", [TRUSTED_CI_WORKFLOW_PATH])
    workflow.write_text(TRUSTED_CI, encoding="utf-8")
    restored_sha = commit_paths(candidate, "ci: restore trusted gates", [TRUSTED_CI_WORKFLOW_PATH])

    comparison = _compare(trusted, candidate, restored_sha)

    assert comparison.status == "trusted"
    assert comparison.differing_paths == ()


def test_new_auto_discovered_gate_config_requires_re_review(tmp_path: Path) -> None:
    trusted, candidate = _repositories(tmp_path)
    config = candidate / ".coveragerc"
    config.write_text("[report]\nfail_under = 0\n", encoding="utf-8")
    candidate_sha = commit_paths(
        candidate,
        "ci: add coverage override",
        [".coveragerc"],
    )

    comparison = _compare(trusted, candidate, candidate_sha)

    assert comparison.status == "requires-re-review"
    assert comparison.trusted_definition_hash != comparison.candidate_definition_hash
    assert comparison.differing_paths == (".coveragerc",)


def test_nested_ruff_config_cannot_weaken_pinned_root_config(tmp_path: Path) -> None:
    trusted, candidate = _repositories(tmp_path)
    nested_config = candidate / "src/ruff.toml"
    nested_config.parent.mkdir(parents=True)
    nested_config.write_text(
        '[lint]\nignore = ["F401"]\n[format]\nquote-style = "single"\n',
        encoding="utf-8",
    )
    bad_source = candidate / "src/bad.py"
    bad_source.write_text("import os\n\nvalue = 'nested single'\n", encoding="utf-8")
    candidate_sha = commit_paths(
        candidate,
        "test: simulate nested Ruff bypass",
        ["src/ruff.toml", "src/bad.py"],
    )

    # Product source remains the subject of the gate, not part of its definition.
    # Safety comes from the hashed Makefile pin, not from broad-hashing src/**.
    assert _compare(trusted, candidate, candidate_sha).status == "trusted"
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    assert makefile.count("ruff check --config pyproject.toml") == 2
    assert makefile.count("ruff format --config pyproject.toml") == 2

    ruff = str(Path(sys.executable).with_name("ruff"))
    discovered_lint = subprocess.run(
        [ruff, "check", "src/bad.py"],
        cwd=candidate,
        check=False,
        capture_output=True,
        text=True,
    )
    discovered_format = subprocess.run(
        [ruff, "format", "--check", "src/bad.py"],
        cwd=candidate,
        check=False,
        capture_output=True,
        text=True,
    )
    pinned_lint = subprocess.run(
        [ruff, "check", "--config", "pyproject.toml", "src/bad.py"],
        cwd=candidate,
        check=False,
        capture_output=True,
        text=True,
    )
    pinned_format = subprocess.run(
        [ruff, "format", "--config", "pyproject.toml", "--check", "src/bad.py"],
        cwd=candidate,
        check=False,
        capture_output=True,
        text=True,
    )

    assert discovered_lint.returncode == 0
    assert discovered_format.returncode == 0
    assert pinned_lint.returncode == 1
    assert "F401" in pinned_lint.stdout
    assert pinned_format.returncode == 1


def test_differently_named_candidate_workflow_cannot_spoof_ci_run(tmp_path: Path) -> None:
    trusted, candidate = _repositories(tmp_path)
    candidate_sha = git(candidate, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(CIWorkflowTrustError, match="source CI run path"):
        compare_ci_definition(
            GitRepository(trusted),
            GitRepository(candidate),
            candidate_sha,
            ".github/workflows/no-op-ci.yml",
        )


def test_manifest_covers_direct_commands_configs_and_recursive_gate_code() -> None:
    assert {
        "Makefile",
        "pyproject.toml",
        "uv.lock",
        "compose.yaml",
        "Dockerfile",
        ".dockerignore",
        ".env.example",
        "alembic.ini",
        "migrations/env.py",
        "migrations/script.py.mako",
    }.issubset(TRUSTED_CI_EXACT_PATHS)
    assert {
        ".github",
        "docker",
        "scripts",
        "tests",
    }.issubset(TRUSTED_CI_TREE_PATHS)


def _compare(
    trusted: Path,
    candidate: Path,
    candidate_sha: str,
) -> CIDefinitionComparison:
    return compare_ci_definition(
        GitRepository(trusted),
        GitRepository(candidate),
        candidate_sha,
        TRUSTED_CI_WORKFLOW_PATH,
    )


def _repositories(tmp_path: Path) -> tuple[Path, Path]:
    trusted = tmp_path / "trusted"
    candidate = tmp_path / "candidate"
    git(tmp_path, "init", "-b", "main", str(trusted))
    git(trusted, "config", "user.name", "AO Test")
    git(trusted, "config", "user.email", "ao@example.invalid")
    files = {
        TRUSTED_CI_WORKFLOW_PATH: TRUSTED_CI,
        "Makefile": (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8"),
        "pyproject.toml": (
            "[tool.coverage.report]\n"
            "fail_under = 90\n"
            "[tool.mypy]\n"
            "strict = true\n"
            "[tool.ruff.lint]\n"
            'select = ["F"]\n'
            "[tool.ruff.format]\n"
            'quote-style = "double"\n'
        ),
        "uv.lock": "version = 1\n",
        "tests/test_gate.py": "def test_gate():\n    assert True\n",
    }
    for path, content in files.items():
        target = trusted / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    commit_paths(trusted, "ci: trusted gate definition", tuple(files))
    git(tmp_path, "clone", str(trusted), str(candidate))
    git(candidate, "config", "user.name", "AO Test")
    git(candidate, "config", "user.email", "ao@example.invalid")
    return trusted, candidate
