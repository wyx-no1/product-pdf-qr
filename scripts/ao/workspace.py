from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scripts.ao.evidence import parse_evidence_binding
from scripts.ao.git import GitRepository, append_json_event
from scripts.ao.github import GitHubData
from scripts.ao.models import EvidenceBinding


class WorkspaceError(RuntimeError):
    """An Advisor workspace cannot be created or safely managed."""


@dataclass(frozen=True)
class StaleWorktree:
    path: Path
    commit_sha: str
    age_seconds: float
    pattern: str
    active_process: bool


@dataclass(frozen=True)
class OpinionValidation:
    valid: bool
    gate_status: str
    reviewed_commit_sha: str
    evidence_commit_sha: str
    current_pr_head_sha: str
    reason: str


class WorkspaceLease:
    def __init__(
        self,
        repository: GitRepository,
        path: Path,
        binding: EvidenceBinding,
        log_path: Path,
        now: Callable[[], datetime],
        lock_fd: int,
    ) -> None:
        self.repository = repository
        self.path = path
        self.binding = binding
        self.log_path = log_path
        self.now = now
        self.closed = False
        self.marker_path = _marker_path(path)
        self.lock_fd: int | None = lock_fd

    def __enter__(self) -> WorkspaceLease:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        removed = self.repository.git(
            "worktree",
            "remove",
            "--force",
            str(self.path),
            check=False,
        )
        pruned = self.repository.git("worktree", "prune", check=False)
        if removed.returncode != 0 and self.path.exists():
            if self._marker_matches():
                shutil.rmtree(self.path)
                pruned = self.repository.git("worktree", "prune", check=False)
        if not self.path.exists():
            self.marker_path.unlink(missing_ok=True)
        if self.path.exists() or self.marker_path.exists() or pruned.returncode != 0:
            append_json_event(
                self.log_path,
                {
                    "action": "advisor.workspace.destroy",
                    "commit_sha": self.binding.code_commit_sha,
                    "error": removed.stderr.strip() or pruned.stderr.strip(),
                    "finished_at": self.now().isoformat(),
                    "path": str(self.path),
                    "pr_number": self.binding.pr_number,
                    "result": "failure",
                },
            )
            raise WorkspaceError(f"failed to clean Advisor worktree {self.path}")
        self.closed = True
        self._release_lock()
        append_json_event(
            self.log_path,
            {
                "action": "advisor.workspace.destroy",
                "commit_sha": self.binding.code_commit_sha,
                "destroyed_at": self.now().isoformat(),
                "path": str(self.path),
                "pr_number": self.binding.pr_number,
                "result": "success",
            },
        )

    def _marker_matches(self) -> bool:
        try:
            data = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(
            data.get("pr_number") == self.binding.pr_number
            and data.get("commit_sha") == self.binding.code_commit_sha
        )

    def record_advisor_process(self, process_id: int) -> None:
        try:
            marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkspaceError("cannot update Advisor workspace lifecycle marker") from error
        if not isinstance(marker, dict):
            raise WorkspaceError("Advisor workspace lifecycle marker is invalid")
        marker["advisor_pid"] = process_id
        marker["advisor_process_group_id"] = process_id
        self.marker_path.write_text(
            json.dumps(marker, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _release_lock(self) -> None:
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


class WorkspaceResolver:
    def __init__(
        self,
        repository: GitRepository,
        *,
        temp_root: Path | None = None,
        log_path: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.temp_root = (temp_root or Path(tempfile.gettempdir())).resolve()
        self.log_path = log_path or repository.common_git_dir() / "ao" / "workspace-events.jsonl"
        self.now = now or (lambda: datetime.now(UTC))

    def acquire(self, metadata_path: Path) -> WorkspaceLease:
        started_at = self.now()
        started = time.monotonic()
        binding: EvidenceBinding | None = None
        path: Path | None = None
        lock_fd: int | None = None
        try:
            binding = parse_evidence_binding(metadata_path)
            # Fetch is deliberately unconditional. A failure pauses review; there is
            # no fallback to main, another branch, or an already-present object.
            self.repository.fetch("origin")
            self.repository.git("cat-file", "-e", f"{binding.code_commit_sha}^{{commit}}")
            lock_fd = self._acquire_lock(binding)
            prefix = _workspace_prefix(self.repository, binding)
            existing = [
                existing_path
                for existing_path, _ in _worktree_entries(self.repository)
                if existing_path.name.startswith(prefix)
            ]
            if existing:
                raise WorkspaceError(
                    f"Advisor worktree already exists for PR #{binding.pr_number} "
                    f"at {existing[0]}; reclaim it before retrying"
                )
            path = self._allocate_path(binding)
            self.repository.git(
                "worktree",
                "add",
                "--detach",
                str(path),
                binding.code_commit_sha,
            )
            actual = self.repository.runner.run(
                ("git", "-C", str(path), "rev-parse", "HEAD")
            ).stdout.strip()
            branch = self.repository.runner.run(
                ("git", "-C", str(path), "branch", "--show-current")
            ).stdout.strip()
            if actual != binding.code_commit_sha or branch:
                raise WorkspaceError("Advisor worktree is not detached at the Evidence code commit")
            marker = {
                "commit_sha": binding.code_commit_sha,
                "created_at": started_at.isoformat(),
                "pid": os.getpid(),
                "pr_number": binding.pr_number,
            }
            _marker_path(path).write_text(
                json.dumps(marker, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            append_json_event(
                self.log_path,
                {
                    "action": "advisor.workspace.create",
                    "commit_sha": binding.code_commit_sha,
                    "created_at": started_at.isoformat(),
                    "duration_seconds": round(time.monotonic() - started, 6),
                    "path": str(path),
                    "pr_number": binding.pr_number,
                    "result": "success",
                },
            )
            return WorkspaceLease(
                self.repository,
                path,
                binding,
                self.log_path,
                self.now,
                lock_fd,
            )
        except BaseException as error:
            if path is not None and path.exists():
                self.repository.git(
                    "worktree",
                    "remove",
                    "--force",
                    str(path),
                    check=False,
                )
                if path.exists():
                    shutil.rmtree(path)
                self.repository.git("worktree", "prune", check=False)
            if path is not None:
                _marker_path(path).unlink(missing_ok=True)
            if lock_fd is not None:
                os.close(lock_fd)
            append_json_event(
                self.log_path,
                {
                    "action": "advisor.workspace.create",
                    "commit_sha": binding.code_commit_sha if binding else None,
                    "duration_seconds": round(time.monotonic() - started, 6),
                    "error": str(error),
                    "finished_at": self.now().isoformat(),
                    "gate_status": "indeterminate",
                    "path": str(path) if path else None,
                    "pr_number": binding.pr_number if binding else None,
                    "result": "failure",
                    "started_at": started_at.isoformat(),
                },
            )
            raise

    def _allocate_path(self, binding: EvidenceBinding) -> Path:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        prefix = _workspace_prefix(self.repository, binding)
        allocated = Path(tempfile.mkdtemp(prefix=prefix, dir=self.temp_root))
        allocated.rmdir()
        return allocated

    def _acquire_lock(self, binding: EvidenceBinding) -> int:
        prefix = _workspace_prefix(self.repository, binding).removesuffix("-")
        lock_path = self.repository.common_git_dir() / "ao" / "locks" / f"{prefix}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(lock_fd)
            raise WorkspaceError(
                f"Advisor workspace for PR #{binding.pr_number} is already active"
            ) from error
        return lock_fd

    def run_advisor(
        self,
        metadata_path: Path,
        command: Sequence[str],
        record_path: Path,
        *,
        timeout_seconds: float,
        terminate_grace_seconds: float = 5.0,
    ) -> int:
        if not command:
            raise WorkspaceError("Advisor command must not be empty")
        if timeout_seconds <= 0:
            raise WorkspaceError("Advisor timeout must be greater than zero")
        if terminate_grace_seconds <= 0:
            raise WorkspaceError("Advisor termination grace period must be greater than zero")
        lease = self.acquire(metadata_path)
        process: subprocess.Popen[str] | None = None
        started_at = self.now()
        status = "interrupted"
        exit_code: int | None = None
        process_return_code: int | None = None
        termination_method: str | None = None
        failure_reason: str | None = None
        cleanup_succeeded = False
        try:
            environment = os.environ.copy()
            environment.update(
                {
                    "AO_ADVISOR_COMMIT_SHA": lease.binding.code_commit_sha,
                    "AO_ADVISOR_PR_NUMBER": str(lease.binding.pr_number),
                    "AO_EVIDENCE_METADATA": str(metadata_path.resolve()),
                }
            )
            process = subprocess.Popen(
                list(command),
                cwd=lease.path,
                env=environment,
                start_new_session=True,
                text=True,
            )
            lease.record_advisor_process(process.pid)
            try:
                process_return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                status = "timed-out"
                exit_code = 124
                failure_reason = f"Advisor exceeded timeout of {timeout_seconds} seconds"
                termination_method = _terminate_process_group(
                    process,
                    terminate_grace_seconds,
                )
            else:
                exit_code = process_return_code
                status = "completed" if exit_code == 0 else "command-failed"
                if exit_code != 0:
                    failure_reason = f"Advisor command exited with status {exit_code}"
        except BaseException as error:
            failure_reason = f"{type(error).__name__}: {error}"
            if process is not None and process.poll() is None:
                termination_method = _terminate_process_group(
                    process,
                    terminate_grace_seconds,
                )
            raise
        finally:
            if process is not None and process.poll() is None:
                termination_method = _terminate_process_group(
                    process,
                    terminate_grace_seconds,
                )
            try:
                lease.close()
                cleanup_succeeded = True
            finally:
                record = {
                    "cleanup_succeeded": cleanup_succeeded,
                    "evidence_metadata": str(metadata_path.resolve()),
                    "exit_code": exit_code,
                    "failure_reason": failure_reason,
                    "finished_at": self.now().isoformat(),
                    "pr_number": lease.binding.pr_number,
                    "process_return_code": (
                        process.returncode if process is not None else process_return_code
                    ),
                    "reviewed_commit_sha": lease.binding.code_commit_sha,
                    "started_at": started_at.isoformat(),
                    "status": status,
                    "termination_method": termination_method,
                    "timeout_seconds": timeout_seconds,
                    "workspace_path": str(lease.path),
                }
                _write_json_atomic(record_path, record)
                append_json_event(
                    self.log_path,
                    {
                        "action": "advisor.run",
                        "cleanup_succeeded": cleanup_succeeded,
                        "commit_sha": lease.binding.code_commit_sha,
                        "failure_reason": failure_reason,
                        "finished_at": self.now().isoformat(),
                        "gate_status": (
                            "valid"
                            if status == "completed" and cleanup_succeeded
                            else "indeterminate"
                        ),
                        "path": str(lease.path),
                        "pr_number": lease.binding.pr_number,
                        "result": (
                            "success" if status == "completed" and cleanup_succeeded else "failure"
                        ),
                        "status": status,
                        "termination_method": termination_method,
                        "timeout_seconds": timeout_seconds,
                    },
                )
        if exit_code is None:
            raise WorkspaceError("Advisor process finished without an exit status")
        return exit_code


def detect_stale_worktrees(
    repository: GitRepository,
    *,
    temp_root: Path | None = None,
    older_than_seconds: float,
    now: Callable[[], datetime] | None = None,
) -> tuple[StaleWorktree, ...]:
    if older_than_seconds < 0:
        raise ValueError("older_than_seconds must be non-negative")
    root = (temp_root or Path(tempfile.gettempdir())).resolve()
    current_time = (now or (lambda: datetime.now(UTC)))().timestamp()
    entries = _worktree_entries(repository)
    project_name = repository.project_name()
    modern = re.compile(rf"^{re.escape(project_name)}-advisor-pr-\d+-[0-9a-f]{{7,40}}-")
    legacy = re.compile(rf"^{re.escape(project_name)}-(?:advisor|g01(?:-final)?)\.")
    stale: list[StaleWorktree] = []
    for path, commit in entries:
        try:
            if path.resolve().parent != root:
                continue
            name = path.name
            if modern.match(name):
                pattern = "advisor"
            elif legacy.match(name):
                pattern = "legacy-advisor-review"
            else:
                continue
            marker = _read_marker(path)
            created_at = _marker_timestamp(marker)
            timestamp = created_at if created_at is not None else path.stat().st_mtime
            age = max(0.0, current_time - timestamp)
            active = _marker_pid_active(marker)
            if age >= older_than_seconds and not active:
                stale.append(StaleWorktree(path, commit, age, pattern, active))
        except FileNotFoundError:
            continue
    return tuple(sorted(stale, key=lambda item: str(item.path)))


def reclaim_stale_worktrees(
    repository: GitRepository,
    stale: Sequence[StaleWorktree],
    *,
    log_path: Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> tuple[Path, ...]:
    event_log = log_path or repository.common_git_dir() / "ao" / "workspace-events.jsonl"
    clock = now or (lambda: datetime.now(UTC))
    reclaimed: list[Path] = []
    for item in stale:
        if item.active_process:
            raise WorkspaceError(f"refusing to reclaim active worktree {item.path}")
        result = repository.git(
            "worktree",
            "remove",
            "--force",
            str(item.path),
            check=False,
        )
        if result.returncode != 0 or item.path.exists():
            append_json_event(
                event_log,
                {
                    "action": "advisor.workspace.reclaim",
                    "commit_sha": item.commit_sha,
                    "error": result.stderr.strip(),
                    "finished_at": clock().isoformat(),
                    "path": str(item.path),
                    "result": "failure",
                },
            )
            raise WorkspaceError(f"failed to reclaim stale worktree {item.path}")
        _marker_path(item.path).unlink(missing_ok=True)
        reclaimed.append(item.path)
        append_json_event(
            event_log,
            {
                "action": "advisor.workspace.reclaim",
                "commit_sha": item.commit_sha,
                "finished_at": clock().isoformat(),
                "path": str(item.path),
                "result": "success",
            },
        )
    repository.git("worktree", "prune")
    return tuple(reclaimed)


def validate_advisor_opinion(
    repository: GitRepository,
    github: GitHubData,
    metadata_path: Path,
    record_path: Path,
) -> OpinionValidation:
    binding = parse_evidence_binding(metadata_path)
    try:
        raw = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"cannot read Advisor opinion record {record_path}") from error
    if not isinstance(raw, dict):
        raise WorkspaceError("Advisor opinion record must be a JSON object")
    reviewed = raw.get("reviewed_commit_sha")
    if not isinstance(reviewed, str):
        raise WorkspaceError("Advisor opinion record lacks reviewed_commit_sha")
    pull_request = github.pull_request(binding.pr_number)
    if (
        pull_request.number != binding.pr_number
        or pull_request.source_branch != binding.source_branch
        or pull_request.target_branch != binding.target_branch
    ):
        raise WorkspaceError("current PR identity or branches do not match Evidence")
    repository.fetch("origin", pull_request.source_branch)
    current_head = pull_request.head_sha
    fetched_head = repository.git(
        "rev-parse",
        f"origin/{pull_request.source_branch}",
    ).stdout.strip()
    if fetched_head != current_head:
        raise WorkspaceError("PR head moved while validating the Advisor opinion")
    if reviewed != binding.code_commit_sha:
        return OpinionValidation(
            False,
            "indeterminate",
            reviewed,
            binding.code_commit_sha,
            current_head,
            "Advisor opinion commit does not match Evidence code commit",
        )
    if (
        raw.get("status") != "completed"
        or raw.get("exit_code") != 0
        or raw.get("cleanup_succeeded") is not True
    ):
        return OpinionValidation(
            False,
            "indeterminate",
            reviewed,
            binding.code_commit_sha,
            current_head,
            "Advisor command or workspace cleanup did not complete successfully",
        )
    ancestor = repository.git(
        "merge-base",
        "--is-ancestor",
        binding.code_commit_sha,
        current_head,
        check=False,
    )
    if ancestor.returncode != 0:
        return OpinionValidation(
            False,
            "indeterminate",
            reviewed,
            binding.code_commit_sha,
            current_head,
            "Evidence code commit is no longer an ancestor of the PR head",
        )
    later_code_commits = repository.git(
        "log",
        "--format=%H",
        f"{binding.code_commit_sha}..{current_head}",
        "--",
        ".",
        ":(exclude)docs/evidence/**",
    )
    if later_code_commits.stdout.strip():
        return OpinionValidation(
            False,
            "indeterminate",
            reviewed,
            binding.code_commit_sha,
            current_head,
            "PR has code changes after the reviewed commit; re-review is required",
        )
    return OpinionValidation(
        True,
        "valid",
        reviewed,
        binding.code_commit_sha,
        current_head,
        "PR head differs, if at all, only by Evidence files",
    )


def _worktree_entries(repository: GitRepository) -> tuple[tuple[Path, str], ...]:
    output = repository.git("worktree", "list", "--porcelain").stdout
    entries: list[tuple[Path, str]] = []
    path: Path | None = None
    commit = ""
    for line in (*output.splitlines(), ""):
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line.startswith("HEAD "):
            commit = line.removeprefix("HEAD ")
        elif not line and path is not None:
            entries.append((path, commit))
            path = None
            commit = ""
    return tuple(entries)


def _read_marker(path: Path) -> dict[str, object] | None:
    try:
        raw = json.loads(_marker_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _marker_timestamp(marker: dict[str, object] | None) -> float | None:
    if marker is None:
        return None
    value = marker.get("created_at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _marker_pid_active(marker: dict[str, object] | None) -> bool:
    if marker is None:
        return False
    for key in ("pid", "advisor_pid"):
        if _pid_active(marker.get(key)):
            return True
    process_group_id = marker.get("advisor_process_group_id")
    if not isinstance(process_group_id, int) or process_group_id <= 0:
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_active(value: object) -> bool:
    pid = value
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _marker_path(worktree_path: Path) -> Path:
    return worktree_path.with_name(f".{worktree_path.name}.ao-workspace.json")


def _terminate_process_group(
    process: subprocess.Popen[str],
    grace_seconds: float,
) -> str:
    if process.poll() is not None:
        return "already-exited"
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return "already-exited"
    try:
        process.wait(timeout=grace_seconds)
        return "sigterm"
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        return "sigkill"


def _workspace_prefix(repository: GitRepository, binding: EvidenceBinding) -> str:
    return (
        f"{repository.project_name()}-advisor-pr-{binding.pr_number}-"
        f"{binding.code_commit_sha[:12]}-"
    )
