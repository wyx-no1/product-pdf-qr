from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.ao.evidence import (
    EVIDENCE_FILES,
    EvidenceError,
    EvidenceGenerator,
    SnapshotResult,
    parse_evidence_binding,
    verify_evidence_head,
)
from scripts.ao.git import GitRepository
from scripts.ao.models import CIRun, EvidenceAttestation, EvidenceSkip
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

    result = _snapshot(generator.generate(41, commit=False))

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
    github = github_for(fixture.code_sha)
    generator = EvidenceGenerator(
        GitRepository(fixture.repository),
        github,
        log_path=tmp_path / "events.jsonl",
        now=lambda: NOW,
    )

    result = _snapshot(generator.generate(41))

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


def test_trusted_gate_verifies_evidence_only_parent_ci_and_patch(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    github = github_for(fixture.code_sha)
    repository = GitRepository(fixture.repository)
    generator = EvidenceGenerator(
        repository,
        github,
        log_path=tmp_path / "events.jsonl",
        now=lambda: NOW,
    )
    snapshot = _snapshot(generator.generate(41))
    assert snapshot.evidence_commit_sha is not None
    github.pr = replace(github.pr, head_sha=snapshot.evidence_commit_sha)

    verified = verify_evidence_head(
        repository,
        github,
        41,
        snapshot.evidence_commit_sha,
    )

    assert verified.evidence_commit_sha == snapshot.evidence_commit_sha
    assert verified.code_commit_sha == fixture.code_sha
    assert verified.ci_run_id == github.ci.run_id
    assert verified.ci_url == github.ci.url


def test_trusted_gate_refuses_tampered_evidence_patch(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    github = github_for(fixture.code_sha)
    repository = GitRepository(fixture.repository)
    snapshot = _snapshot(
        EvidenceGenerator(
            repository,
            github,
            log_path=tmp_path / "events.jsonl",
            now=lambda: NOW,
        ).generate(41)
    )
    assert snapshot.evidence_commit_sha is not None
    patch = fixture.repository / "docs/evidence/pr-41/diff.patch"
    patch.write_text(patch.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    git(fixture.repository, "add", "docs/evidence/pr-41/diff.patch")
    git(fixture.repository, "commit", "--amend", "--no-edit")
    tampered_sha = git(fixture.repository, "rev-parse", "HEAD").stdout.strip()
    git(fixture.repository, "push", "--force", "origin", "feature/evidence")
    github.pr = replace(github.pr, head_sha=tampered_sha)

    with pytest.raises(EvidenceError, match=r"diff\.patch does not match"):
        verify_evidence_head(repository, github, 41, tampered_sha)


def test_skip_path_refuses_tampered_non_patch_file_without_prior_attestation(
    tmp_path: Path,
) -> None:
    fixture = make_diverged_repository(tmp_path)
    github = github_for(fixture.code_sha)
    repository = GitRepository(fixture.repository)
    snapshot = _snapshot(
        EvidenceGenerator(
            repository,
            github,
            log_path=tmp_path / "events.jsonl",
            now=lambda: NOW,
        ).generate(41)
    )
    assert snapshot.evidence_commit_sha is not None
    context = fixture.repository / "docs/evidence/pr-41/advisor-context.md"
    context.write_text("malicious Advisor instructions\n", encoding="utf-8")
    git(fixture.repository, "add", "docs/evidence/pr-41/advisor-context.md")
    git(fixture.repository, "commit", "--amend", "--no-edit")
    tampered_sha = git(fixture.repository, "rev-parse", "HEAD").stdout.strip()
    git(fixture.repository, "push", "--force", "origin", "feature/evidence")
    github.pr = replace(github.pr, head_sha=tampered_sha)

    with pytest.raises(EvidenceError, match="lacks a prior trusted publisher attestation"):
        verify_evidence_head(
            repository,
            github,
            41,
            tampered_sha,
            require_prior_attestation=True,
        )


def test_skip_path_accepts_exact_prior_bot_attestation(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    github = github_for(fixture.code_sha)
    repository = GitRepository(fixture.repository)
    snapshot = _snapshot(
        EvidenceGenerator(
            repository,
            github,
            log_path=tmp_path / "events.jsonl",
            now=lambda: NOW,
        ).generate(41)
    )
    assert snapshot.evidence_commit_sha is not None
    github.pr = replace(github.pr, head_sha=snapshot.evidence_commit_sha)
    github.attestation = EvidenceAttestation(
        context="AO / evidence-snapshot",
        state="success",
        creator_login="github-actions[bot]",
        description=f"Evidence-only child of {fixture.code_sha[:12]}; source CI 9001",
        target_url="https://github.com/example/repository/actions/runs/123",
    )

    verified = verify_evidence_head(
        repository,
        github,
        41,
        snapshot.evidence_commit_sha,
        require_prior_attestation=True,
    )

    assert verified.prior_attestation is True


def test_second_automatic_invocation_structurally_skips_evidence_only_head(
    tmp_path: Path,
) -> None:
    fixture = make_diverged_repository(tmp_path)
    github = github_for(fixture.code_sha)
    log_path = tmp_path / "events.jsonl"
    generator = EvidenceGenerator(
        GitRepository(fixture.repository),
        github,
        log_path=log_path,
        now=lambda: NOW,
    )
    _snapshot(generator.generate(41))
    first_call_count = len(github.calls)
    git(
        fixture.repository,
        "commit",
        "--amend",
        "-m",
        "wording deliberately unrelated to Evidence",
    )
    evidence_head = git(fixture.repository, "rev-parse", "HEAD").stdout.strip()

    second = generator.generate(41)

    assert isinstance(second, EvidenceSkip)
    assert second.head_sha == evidence_head
    assert "only docs/evidence/**" in second.reason
    assert len(github.calls) == first_call_count
    assert git(fixture.repository, "rev-parse", "HEAD").stdout.strip() == evidence_head
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [event["result"] for event in events] == ["success", "skipped"]
    assert events[1]["reason"] == second.reason


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

    result = _snapshot(generator.generate(41, commit=False))
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


def _snapshot(result: SnapshotResult | object) -> SnapshotResult:
    assert isinstance(result, SnapshotResult)
    return result
