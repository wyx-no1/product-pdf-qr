from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.ao.evidence import (
    EVIDENCE_FILES,
    EvidenceError,
    EvidenceGenerator,
    parse_evidence_binding,
)
from scripts.ao.git import GitRepository
from scripts.ao.models import CIRun
from tests.ao.helpers import (
    FakeGitHubData,
    commit_paths,
    git,
    github_for,
    make_diverged_repository,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def test_snapshot_uses_three_dot_diff_and_contains_exact_files(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    log_path = tmp_path / "evidence-events.jsonl"
    generator = EvidenceGenerator(
        GitRepository(fixture.repository),
        github_for(fixture.code_sha),
        log_path=log_path,
        now=lambda: NOW,
    )

    result = generator.generate(41, commit=False)

    assert {path.name for path in result.directory.iterdir()} == EVIDENCE_FILES
    expected = git(
        fixture.repository,
        "diff",
        f"origin/main...{fixture.code_sha}",
        "--",
        ".",
        ":(exclude)docs/evidence/**",
    ).stdout
    assert (result.directory / "diff.patch").read_text(encoding="utf-8") == expected
    assert "main-only.txt" not in expected
    assert not any(path.suffix == ".py" for path in result.directory.iterdir())

    metadata = (result.directory / "metadata.md").read_text(encoding="utf-8")
    validation = (result.directory / "validation.md").read_text(encoding="utf-8")
    assert f"origin/main...{fixture.code_sha}" in metadata
    assert ":(exclude)docs/evidence/**" in metadata
    assert "not to the later\n> Evidence commit" in metadata
    assert "not to the Evidence commit" in validation
    assert parse_evidence_binding(result.directory / "metadata.md").code_commit_sha == (
        fixture.code_sha
    )

    changed = (result.directory / "changed-files.md").read_text(encoding="utf-8")
    assert "`feature.txt`" in changed
    assert "`main-only.txt`" not in changed
    assert "origin/main..." in changed

    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert events == [
        {
            "action": "evidence.generate",
            "ci_run_id": 9001,
            "code_commit_sha": fixture.code_sha,
            "duration_seconds": events[0]["duration_seconds"],
            "evidence_commit_sha": None,
            "finished_at": NOW.isoformat(),
            "gate_status": "ready-for-review",
            "pr_number": 41,
            "pushed": False,
            "result": "success",
            "started_at": NOW.isoformat(),
        }
    ]


def test_snapshot_is_a_separate_evidence_only_commit(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    generator = EvidenceGenerator(
        GitRepository(fixture.repository),
        github_for(fixture.code_sha),
        log_path=tmp_path / "events.jsonl",
        now=lambda: NOW,
    )

    result = generator.generate(41)

    assert result.evidence_commit_sha is not None
    assert git(fixture.repository, "rev-parse", "HEAD^").stdout.strip() == fixture.code_sha
    assert (
        git(fixture.repository, "rev-parse", "origin/feature/evidence").stdout.strip()
        == result.evidence_commit_sha
    )
    changed = set(
        git(
            fixture.repository,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "HEAD",
        ).stdout.splitlines()
    )
    assert changed == {f"docs/evidence/pr-41/{name}" for name in EVIDENCE_FILES}
    assert (
        git(fixture.repository, "log", "-1", "--format=%s").stdout.strip()
        == "docs: add PR41 review evidence"
    )


def test_regeneration_excludes_previous_evidence_from_patch(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    evidence_directory = fixture.repository / "docs/evidence/pr-41"
    evidence_directory.mkdir(parents=True)
    for name in EVIDENCE_FILES:
        (evidence_directory / name).write_text("old evidence\n", encoding="utf-8")
    commit_paths(
        fixture.repository,
        "docs: old evidence",
        [f"docs/evidence/pr-41/{name}" for name in EVIDENCE_FILES],
    )
    (fixture.repository / "next-code.txt").write_text("next\n", encoding="utf-8")
    code_sha = commit_paths(
        fixture.repository,
        "feat: update code",
        ["next-code.txt"],
    )
    git(fixture.repository, "push", "origin", "feature/evidence")
    generator = EvidenceGenerator(
        GitRepository(fixture.repository),
        github_for(code_sha),
        log_path=tmp_path / "events.jsonl",
        now=lambda: NOW,
    )

    result = generator.generate(41, commit=False)
    patch = (result.directory / "diff.patch").read_text(encoding="utf-8")

    assert "next-code.txt" in patch
    assert "docs/evidence/" not in patch
    assert {path.name for path in result.directory.iterdir()} == EVIDENCE_FILES


def test_snapshot_refuses_unsuccessful_ci_and_records_indeterminate(
    tmp_path: Path,
) -> None:
    fixture = make_diverged_repository(tmp_path)
    github = github_for(fixture.code_sha)
    failed = CIRun(
        run_id=9002,
        commit_sha=fixture.code_sha,
        status="completed",
        conclusion="failure",
        url="https://example.invalid/actions/9002",
        jobs=github.ci.jobs,
    )
    provider = FakeGitHubData(github.pr, failed)
    log_path = tmp_path / "failure.jsonl"
    generator = EvidenceGenerator(
        GitRepository(fixture.repository),
        provider,
        log_path=log_path,
        now=lambda: NOW,
    )

    with pytest.raises(EvidenceError, match="not completed successfully"):
        generator.generate(41, commit=False)

    assert not (fixture.repository / "docs/evidence/pr-41").exists()
    event = json.loads(log_path.read_text(encoding="utf-8"))
    assert event["result"] == "failure"
    assert event["gate_status"] == "indeterminate"
    assert event["ci_run_id"] == 9002


def test_snapshot_refuses_dirty_or_wrong_commit(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    repository = GitRepository(fixture.repository)
    generator = EvidenceGenerator(
        repository,
        github_for(fixture.code_sha),
        log_path=tmp_path / "events.jsonl",
        now=lambda: NOW,
    )
    (fixture.repository / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(EvidenceError, match="clean code commit"):
        generator.generate(41, commit=False)


def test_snapshot_stops_when_remote_pr_branch_moves(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    competing = tmp_path / "competing"
    git(tmp_path, "clone", str(fixture.origin), str(competing))
    git(competing, "config", "user.name", "AO Test")
    git(competing, "config", "user.email", "ao@example.invalid")
    git(competing, "switch", "feature/evidence")
    (competing / "concurrent.txt").write_text("newer\n", encoding="utf-8")
    commit_paths(competing, "feat: concurrent update", ["concurrent.txt"])
    git(competing, "push", "origin", "feature/evidence")
    generator = EvidenceGenerator(
        GitRepository(fixture.repository),
        github_for(fixture.code_sha),
        log_path=tmp_path / "events.jsonl",
        now=lambda: NOW,
    )

    with pytest.raises(EvidenceError, match="remote PR branch moved"):
        generator.generate(41, commit=False)

    assert not (fixture.repository / "docs/evidence/pr-41").exists()


def test_binding_parser_requires_all_four_bindings(tmp_path: Path) -> None:
    metadata = tmp_path / "metadata.md"
    metadata.write_text(
        """| PR number | `#41` |
| Source branch | `feature/evidence` |
| Target branch | `main` |
| Code commit SHA | `0000000000000000000000000000000000000000` |
| CI run ID | `9001` |
""",
        encoding="utf-8",
    )

    with pytest.raises(EvidenceError, match="CI conclusion"):
        parse_evidence_binding(metadata)
