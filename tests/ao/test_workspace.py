from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.ao.evidence import EvidenceGenerator, SnapshotResult
from scripts.ao.git import CommandFailed, GitRepository
from scripts.ao.models import EvidenceAttestation
from scripts.ao.workspace import (
    WorkspaceError,
    WorkspaceResolver,
    detect_stale_worktrees,
    reclaim_stale_worktrees,
    validate_advisor_opinion,
)
from tests.ao.helpers import (
    FakeGitHubData,
    GitFixture,
    commit_paths,
    git,
    github_for,
    make_diverged_repository,
    write_metadata,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def test_resolver_uses_evidence_sha_in_detached_worktree_and_cleans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_diverged_repository(tmp_path)
    metadata = write_metadata(tmp_path / "outside/metadata.md", fixture.code_sha)
    temp_root = tmp_path / "advisor-worktrees"
    resolver = WorkspaceResolver(
        GitRepository(fixture.repository),
        temp_root=temp_root,
        log_path=tmp_path / "workspace-events.jsonl",
        now=lambda: NOW,
    )
    monkeypatch.chdir(tmp_path)

    with resolver.acquire(metadata) as lease:
        path = lease.path
        assert f"advisor-pr-41-{fixture.code_sha[:12]}-" in path.name
        assert git(path, "rev-parse", "HEAD").stdout.strip() == fixture.code_sha
        assert git(path, "branch", "--show-current").stdout.strip() == ""
        assert git(path, "status", "--porcelain").stdout == ""
        assert (path / "feature.txt").read_text(encoding="utf-8") == "feature\n"

    assert not path.exists()
    registered = git(fixture.repository, "worktree", "list", "--porcelain").stdout
    assert str(path) not in registered
    events = [
        json.loads(line)
        for line in (tmp_path / "workspace-events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["action"] for event in events] == [
        "advisor.workspace.create",
        "advisor.workspace.destroy",
    ]
    assert all(event["commit_sha"] == fixture.code_sha for event in events)


def test_fetch_failure_stops_without_fallback_worktree(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    metadata = write_metadata(tmp_path / "metadata.md", fixture.code_sha)
    git(
        fixture.repository,
        "remote",
        "set-url",
        "origin",
        str(tmp_path / "missing-origin.git"),
    )
    temp_root = tmp_path / "advisor-worktrees"
    log_path = tmp_path / "events.jsonl"
    resolver = WorkspaceResolver(
        GitRepository(fixture.repository),
        temp_root=temp_root,
        log_path=log_path,
        now=lambda: NOW,
    )

    with pytest.raises(CommandFailed):
        resolver.acquire(metadata)

    assert not temp_root.exists() or not tuple(temp_root.iterdir())
    event = json.loads(log_path.read_text(encoding="utf-8"))
    assert event["result"] == "failure"
    assert event["gate_status"] == "indeterminate"
    assert event["path"] is None


def test_same_pr_and_commit_cannot_create_duplicate_active_workspace(
    tmp_path: Path,
) -> None:
    fixture = make_diverged_repository(tmp_path)
    metadata = write_metadata(tmp_path / "metadata.md", fixture.code_sha)
    temp_root = tmp_path / "advisor-worktrees"
    first = WorkspaceResolver(
        GitRepository(fixture.repository),
        temp_root=temp_root,
        log_path=tmp_path / "first-events.jsonl",
        now=lambda: NOW,
    )
    second = WorkspaceResolver(
        GitRepository(fixture.repository),
        temp_root=temp_root,
        log_path=tmp_path / "second-events.jsonl",
        now=lambda: NOW,
    )

    with first.acquire(metadata):
        with pytest.raises(WorkspaceError, match="already active"):
            second.acquire(metadata)

    with second.acquire(metadata) as lease:
        assert lease.path.exists()


def test_advisor_run_records_actual_sha_and_cleans_after_command(
    tmp_path: Path,
) -> None:
    fixture = make_diverged_repository(tmp_path)
    metadata, evidence_head = _publish_trusted_evidence(fixture, tmp_path)
    record = tmp_path / "advisor-record.json"
    observed = tmp_path / "observed.txt"
    resolver = WorkspaceResolver(
        GitRepository(fixture.repository),
        temp_root=tmp_path / "advisor-worktrees",
        log_path=tmp_path / "events.jsonl",
        now=lambda: NOW,
    )
    command = (
        sys.executable,
        "-c",
        (
            "import os,pathlib; "
            f"pathlib.Path({str(observed)!r}).write_text("
            "os.environ['AO_ADVISOR_COMMIT_SHA'])"
        ),
    )

    assert resolver.run_advisor(metadata, command, record, timeout_seconds=5) == 0

    data = json.loads(record.read_text(encoding="utf-8"))
    assert data["reviewed_commit_sha"] == fixture.code_sha
    assert data["evidence"]["commit_sha"] == evidence_head
    assert data["evidence"]["code_commit_sha"] == fixture.code_sha
    assert data["status"] == "completed"
    assert data["cleanup_succeeded"] is True
    assert data["workspace_lifecycle"]["destroy_reason"] == "normal"
    assert data["workspace_lifecycle"]["detached"] is True
    assert observed.read_text(encoding="utf-8") == fixture.code_sha
    assert not Path(data["workspace_path"]).exists()


def test_timeout_kills_process_group_before_worktree_and_lock_cleanup(
    tmp_path: Path,
) -> None:
    fixture = make_diverged_repository(tmp_path)
    metadata = write_metadata(tmp_path / "metadata.md", fixture.code_sha)
    _publish_evidence_metadata(fixture.repository, metadata)
    record = tmp_path / "timeout-record.json"
    pid_file = tmp_path / "advisor.pid"
    event_log = tmp_path / "events.jsonl"
    resolver = WorkspaceResolver(
        GitRepository(fixture.repository),
        temp_root=tmp_path / "advisor-worktrees",
        log_path=event_log,
        now=lambda: NOW,
    )
    command = (
        sys.executable,
        "-c",
        (
            "import os,pathlib,signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
            "time.sleep(30)"
        ),
    )

    result = resolver.run_advisor(
        metadata,
        command,
        record,
        timeout_seconds=0.5,
        terminate_grace_seconds=0.2,
    )

    assert result == 124
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    data = json.loads(record.read_text(encoding="utf-8"))
    assert data["status"] == "timed-out"
    assert data["termination_method"] == "sigkill"
    assert data["timeout_seconds"] == 0.5
    assert data["cleanup_succeeded"] is True
    assert data["workspace_lifecycle"]["destroy_reason"] == "timeout"
    assert "exceeded timeout" in data["failure_reason"]
    assert not Path(data["workspace_path"]).exists()
    events = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["action"] == "advisor.run"
    assert events[-1]["gate_status"] == "indeterminate"
    assert events[-1]["termination_method"] == "sigkill"

    with resolver.acquire(metadata) as lease:
        assert lease.path.exists()


def test_sigterm_interrupt_still_cleans_worktree(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    metadata = write_metadata(tmp_path / "metadata.md", fixture.code_sha)
    _publish_evidence_metadata(fixture.repository, metadata)
    record = tmp_path / "interrupted-record.json"
    started = tmp_path / "advisor-started"
    temp_root = tmp_path / "advisor-worktrees"
    project_root = Path(__file__).resolve().parents[2]
    process = subprocess.Popen(
        (
            sys.executable,
            "-m",
            "scripts.ao",
            "advisor-run",
            "--repo",
            str(fixture.repository),
            "--metadata",
            str(metadata),
            "--record",
            str(record),
            "--temp-root",
            str(temp_root),
            "--",
            sys.executable,
            "-c",
            (
                "import pathlib,time; "
                f"pathlib.Path({str(started)!r}).write_text('started'); "
                "time.sleep(30)"
            ),
        ),
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if started.exists():
            break
        if process.poll() is not None:
            pytest.fail(f"advisor process exited early: {process.communicate()}")
        time.sleep(0.05)
    else:
        process.kill()
        pytest.fail("Advisor worktree was not created")

    process.terminate()
    _, stderr = process.communicate(timeout=10)

    assert process.returncode == 1, stderr
    data = json.loads(record.read_text(encoding="utf-8"))
    assert data["status"] == "interrupted"
    assert data["cleanup_succeeded"] is True
    assert data["workspace_lifecycle"]["destroy_reason"] == "exception"
    assert not Path(data["workspace_path"]).exists()
    assert "advisor-pr-41-" not in git(fixture.repository, "worktree", "list", "--porcelain").stdout


def test_sigkill_releases_kernel_lock_but_leaves_detectable_worktree(
    tmp_path: Path,
) -> None:
    fixture = make_diverged_repository(tmp_path)
    repository = GitRepository(fixture.repository)
    metadata = write_metadata(tmp_path / "metadata.md", fixture.code_sha)
    _publish_evidence_metadata(fixture.repository, metadata)
    record = tmp_path / "killed-record.json"
    pid_file = tmp_path / "advisor.pid"
    temp_root = tmp_path / "advisor-worktrees"
    project_root = Path(__file__).resolve().parents[2]
    process = subprocess.Popen(
        (
            sys.executable,
            "-m",
            "scripts.ao",
            "advisor-run",
            "--repo",
            str(fixture.repository),
            "--metadata",
            str(metadata),
            "--record",
            str(record),
            "--temp-root",
            str(temp_root),
            "--timeout-seconds",
            "30",
            "--",
            sys.executable,
            "-c",
            (
                "import os,pathlib,time; "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                "time.sleep(30)"
            ),
        ),
        cwd=project_root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not pid_file.exists():
        if process.poll() is not None:
            pytest.fail(f"advisor parent exited early: {process.communicate()}")
        time.sleep(0.05)
    assert pid_file.exists()
    child_pid = int(pid_file.read_text(encoding="utf-8"))

    process.kill()
    process.wait(timeout=5)

    assert process.returncode is not None and process.returncode < 0
    assert not record.exists()
    registered = git(fixture.repository, "worktree", "list", "--porcelain").stdout
    assert "advisor-pr-41-" in registered
    lock_path = (
        repository.common_git_dir()
        / "ao"
        / "locks"
        / (f"{repository.project_name()}-advisor-pr-41-{fixture.code_sha[:12]}.lock")
    )
    lock_fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(lock_fd)

    active_stale = detect_stale_worktrees(
        repository,
        temp_root=temp_root,
        older_than_seconds=0,
    )
    assert active_stale == ()

    os.killpg(child_pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    stale = detect_stale_worktrees(
        repository,
        temp_root=temp_root,
        older_than_seconds=0,
    )
    assert len(stale) == 1
    assert "advisor-pr-41-" in stale[0].path.name
    reclaim_stale_worktrees(repository, stale, log_path=tmp_path / "reclaim.jsonl")
    assert not stale[0].path.exists()


def test_stale_legacy_worktree_detection_and_reclaim(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    repository = GitRepository(fixture.repository)
    temp_root = tmp_path / "temporary"
    temp_root.mkdir()
    legacy = tuple(
        temp_root / f"{fixture.repository.name}-{suffix}"
        for suffix in ("advisor.ABC123", "g01-final.DEF456", "g01.GHI789")
    )
    old = (NOW - timedelta(days=3)).timestamp()
    for path in legacy:
        git(
            fixture.repository,
            "worktree",
            "add",
            "--detach",
            str(path),
            fixture.code_sha,
        )
        os.utime(path, (old, old))

    stale = detect_stale_worktrees(
        repository,
        temp_root=temp_root,
        older_than_seconds=24 * 3600,
        now=lambda: NOW,
    )

    assert {item.path for item in stale} == set(legacy)
    assert {item.pattern for item in stale} == {"legacy-advisor-review"}
    reclaimed = reclaim_stale_worktrees(
        repository,
        stale,
        log_path=tmp_path / "reclaim.jsonl",
        now=lambda: NOW,
    )
    assert set(reclaimed) == set(legacy)
    assert all(not path.exists() for path in legacy)


def test_live_pid_marker_is_not_reported_stale(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    repository = GitRepository(fixture.repository)
    temp_root = tmp_path / "temporary"
    temp_root.mkdir()
    modern = temp_root / f"{fixture.repository.name}-advisor-pr-41-{fixture.code_sha[:12]}-ACTIVE"
    git(
        fixture.repository,
        "worktree",
        "add",
        "--detach",
        str(modern),
        fixture.code_sha,
    )
    marker = {
        "commit_sha": fixture.code_sha,
        "created_at": (NOW - timedelta(days=3)).isoformat(),
        "pid": os.getpid(),
        "pr_number": 41,
    }
    modern.with_name(f".{modern.name}.ao-workspace.json").write_text(
        json.dumps(marker),
        encoding="utf-8",
    )

    try:
        assert (
            detect_stale_worktrees(
                repository,
                temp_root=temp_root,
                older_than_seconds=24 * 3600,
                now=lambda: NOW,
            )
            == ()
        )
    finally:
        git(fixture.repository, "worktree", "remove", "--force", str(modern))
        modern.with_name(f".{modern.name}.ao-workspace.json").unlink()


def test_evidence_resolver_opinion_validation_normal_path_and_pr_update(
    tmp_path: Path,
) -> None:
    fixture = make_diverged_repository(tmp_path)
    metadata, evidence_head = _publish_trusted_evidence(fixture, tmp_path)
    record = tmp_path / "record.json"
    resolver = WorkspaceResolver(
        GitRepository(fixture.repository),
        temp_root=tmp_path / "advisor-worktrees",
        log_path=tmp_path / "workspace-events.jsonl",
        now=lambda: NOW,
    )
    assert (
        resolver.run_advisor(
            metadata,
            (sys.executable, "-c", "raise SystemExit(0)"),
            record,
            timeout_seconds=5,
        )
        == 0
    )
    provider = _provider_with_head(fixture.code_sha, evidence_head)
    validation_log = tmp_path / "validation-events.jsonl"

    valid = validate_advisor_opinion(
        GitRepository(fixture.repository),
        provider,
        metadata,
        record,
        log_path=validation_log,
    )
    assert valid.valid is True
    assert valid.gate_status == "valid"
    assert valid.evidence_commit_sha == evidence_head
    assert valid.current_pr_head_sha == evidence_head
    assert valid.failed_checks == ()
    assert "evidence_attestation" in provider.calls
    valid_event = json.loads(validation_log.read_text(encoding="utf-8"))
    assert valid_event["result"] == "success"

    (fixture.repository / "later-code.txt").write_text("changed\n", encoding="utf-8")
    later_head = commit_paths(
        fixture.repository,
        "feat: later code",
        ["later-code.txt"],
    )
    git(fixture.repository, "push", "origin", "feature/evidence")
    stale = validate_advisor_opinion(
        GitRepository(fixture.repository),
        _provider_with_head(fixture.code_sha, later_head),
        metadata,
        record,
    )
    assert stale.valid is False
    assert stale.gate_status == "indeterminate"
    assert stale.failed_checks == ("current_pr_head",)
    assert "re-review" in stale.reason


def test_opinion_validation_rejects_default_main_workspace_b1(tmp_path: Path) -> None:
    fixture, metadata, record, evidence_head = _prepare_validation_case(tmp_path)
    data = json.loads(record.read_text(encoding="utf-8"))
    data["workspace_path"] = str(fixture.repository)
    data["workspace_lifecycle"]["path"] = str(fixture.repository)
    record.write_text(json.dumps(data), encoding="utf-8")

    result = validate_advisor_opinion(
        GitRepository(fixture.repository),
        _provider_with_head(fixture.code_sha, evidence_head),
        metadata,
        record,
        log_path=tmp_path / "validation-events.jsonl",
    )

    assert result.valid is False
    assert result.gate_status == "indeterminate"
    assert result.failed_checks == ("workspace_lifecycle",)
    assert "Default" in result.reason


def test_opinion_validation_rejects_sha_mismatch_b2(tmp_path: Path) -> None:
    fixture, metadata, record, evidence_head = _prepare_validation_case(tmp_path)
    data = json.loads(record.read_text(encoding="utf-8"))
    data["reviewed_commit_sha"] = fixture.base_sha
    record.write_text(json.dumps(data), encoding="utf-8")

    result = validate_advisor_opinion(
        GitRepository(fixture.repository),
        _provider_with_head(fixture.code_sha, evidence_head),
        metadata,
        record,
        log_path=tmp_path / "validation-events.jsonl",
    )

    assert result.valid is False
    assert result.gate_status == "indeterminate"
    assert result.failed_checks == ("commit_binding",)
    assert "does not match" in result.reason


def test_opinion_validation_rejects_missing_evidence_b3(tmp_path: Path) -> None:
    fixture, metadata, record, evidence_head = _prepare_validation_case(tmp_path)
    data = json.loads(record.read_text(encoding="utf-8"))
    data["evidence"]["commit_sha"] = "0" * 40
    record.write_text(json.dumps(data), encoding="utf-8")
    validation_log = tmp_path / "validation-events.jsonl"

    result = validate_advisor_opinion(
        GitRepository(fixture.repository),
        _provider_with_head(fixture.code_sha, evidence_head),
        metadata,
        record,
        log_path=validation_log,
    )

    assert result.valid is False
    assert result.gate_status == "indeterminate"
    assert result.failed_checks == ("evidence",)
    assert "does not exist" in result.reason
    event = json.loads(validation_log.read_text(encoding="utf-8"))
    assert event["failed_checks"] == ["evidence"]
    assert event["result"] == "failure"


def test_opinion_validation_rejects_missing_lifecycle_b4(tmp_path: Path) -> None:
    fixture, metadata, record, evidence_head = _prepare_validation_case(tmp_path)
    data = json.loads(record.read_text(encoding="utf-8"))
    del data["workspace_lifecycle"]["destroyed_at"]
    record.write_text(json.dumps(data), encoding="utf-8")

    result = validate_advisor_opinion(
        GitRepository(fixture.repository),
        _provider_with_head(fixture.code_sha, evidence_head),
        metadata,
        record,
        log_path=tmp_path / "validation-events.jsonl",
    )

    assert result.valid is False
    assert result.gate_status == "indeterminate"
    assert result.failed_checks == ("workspace_lifecycle",)
    assert "destroyed_at" in result.reason


def test_opinion_validation_rejects_metadata_only_evidence_commit(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    metadata = write_metadata(tmp_path / "metadata.md", fixture.code_sha)
    evidence_head = _publish_evidence_metadata(fixture.repository, metadata)
    record = _write_valid_opinion_record(tmp_path, fixture, evidence_head)

    result = validate_advisor_opinion(
        GitRepository(fixture.repository),
        _provider_with_head(fixture.code_sha, evidence_head),
        metadata,
        record,
        log_path=tmp_path / "validation-events.jsonl",
    )

    assert result.valid is False
    assert result.gate_status == "indeterminate"
    assert result.failed_checks == ("evidence",)
    assert "exactly the five files" in result.reason


def test_opinion_validation_rejects_unattested_evidence_snapshot(tmp_path: Path) -> None:
    fixture = make_diverged_repository(tmp_path)
    metadata, evidence_head = _publish_trusted_evidence(fixture, tmp_path)
    record = _write_valid_opinion_record(tmp_path, fixture, evidence_head)
    provider = github_for(fixture.code_sha)
    provider.pr = replace(provider.pr, head_sha=evidence_head)

    result = validate_advisor_opinion(
        GitRepository(fixture.repository),
        provider,
        metadata,
        record,
        log_path=tmp_path / "validation-events.jsonl",
    )

    assert result.valid is False
    assert result.gate_status == "indeterminate"
    assert result.failed_checks == ("evidence",)
    assert "trusted publisher attestation" in result.reason


def _prepare_validation_case(
    tmp_path: Path,
) -> tuple[GitFixture, Path, Path, str]:
    fixture = make_diverged_repository(tmp_path)
    metadata, evidence_head = _publish_trusted_evidence(fixture, tmp_path)
    record = _write_valid_opinion_record(tmp_path, fixture, evidence_head)
    return fixture, metadata, record, evidence_head


def _write_valid_opinion_record(
    tmp_path: Path,
    fixture: GitFixture,
    evidence_head: str,
) -> Path:
    record = tmp_path / "record.json"
    workspace_path = (
        tmp_path
        / "advisor-worktrees"
        / f"{fixture.repository.name}-advisor-pr-41-{fixture.code_sha[:12]}-complete"
    )
    record.write_text(
        json.dumps(
            {
                "cleanup_succeeded": True,
                "evidence": {
                    "code_commit_sha": fixture.code_sha,
                    "commit_sha": evidence_head,
                    "metadata_path": "docs/evidence/pr-41/metadata.md",
                    "pr_number": 41,
                },
                "exit_code": 0,
                "pr_number": 41,
                "reviewed_commit_sha": fixture.code_sha,
                "status": "completed",
                "workspace_path": str(workspace_path),
                "workspace_lifecycle": {
                    "commit_sha": fixture.code_sha,
                    "created_at": NOW.isoformat(),
                    "destroy_reason": "normal",
                    "destroyed_at": (NOW + timedelta(minutes=1)).isoformat(),
                    "detached": True,
                    "path": str(workspace_path),
                    "pr_number": 41,
                },
            }
        ),
        encoding="utf-8",
    )
    return record


def _publish_trusted_evidence(
    fixture: GitFixture,
    tmp_path: Path,
) -> tuple[Path, str]:
    provider = github_for(fixture.code_sha)
    result = EvidenceGenerator(
        GitRepository(fixture.repository),
        provider,
        log_path=tmp_path / "evidence-events.jsonl",
        now=lambda: NOW,
    ).generate(41)
    assert isinstance(result, SnapshotResult)
    assert result.evidence_commit_sha is not None
    return result.directory / "metadata.md", result.evidence_commit_sha


def _publish_evidence_metadata(repository: Path, metadata: Path) -> str:
    evidence_file = repository / "docs/evidence/pr-41/metadata.md"
    evidence_file.parent.mkdir(parents=True, exist_ok=True)
    evidence_file.write_text(metadata.read_text(encoding="utf-8"), encoding="utf-8")
    evidence_head = commit_paths(
        repository,
        "docs: evidence",
        ["docs/evidence/pr-41/metadata.md"],
    )
    git(repository, "push", "origin", "feature/evidence")
    return evidence_head


def _provider_with_head(code_sha: str, head_sha: str) -> FakeGitHubData:
    provider = github_for(code_sha)
    provider.pr = replace(provider.pr, head_sha=head_sha)
    provider.attestation = EvidenceAttestation(
        context="AO / evidence-snapshot",
        state="success",
        creator_id=41898282,
        creator_login="github-actions[bot]",
        creator_type="Bot",
        description=f"Evidence-only child of {code_sha[:12]}; source CI 9001",
        target_url="https://github.com/example/repository/actions/runs/123",
    )
    return provider
