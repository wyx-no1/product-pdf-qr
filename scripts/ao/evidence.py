from __future__ import annotations

import re
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from scripts.ao.git import GitRepository, append_json_event
from scripts.ao.github import GitHubData, GitHubError
from scripts.ao.models import CIRun, EvidenceBinding, EvidenceSkip, PullRequest, ReviewEvidence

EVIDENCE_FILES = {
    "metadata.md",
    "changed-files.md",
    "diff.patch",
    "validation.md",
    "advisor-context.md",
}
EVIDENCE_EXCLUSION = ":(exclude)docs/evidence/**"
EVIDENCE_STATUS_CONTEXT = "AO / evidence-snapshot"
PROTECTED_PATHS = (
    "migrations/",
    "docs/requirements-v1.md",
    "docs/requirements-v2.md",
    "CLAUDE.md",
    "docs/quality-gates-v1.md",
    "docs/advisor-protocol-v1.md",
    "docs/decision-register-v1.md",
    "docs/test-plan-v1.md",
)


class EvidenceError(RuntimeError):
    """Evidence generation cannot safely continue."""


@dataclass(frozen=True)
class SnapshotResult:
    directory: Path
    code_commit_sha: str
    ci_run_id: int
    evidence_commit_sha: str | None


@dataclass(frozen=True)
class VerifiedEvidenceHead:
    evidence_commit_sha: str
    code_commit_sha: str
    ci_run_id: int
    ci_url: str
    prior_attestation: bool


@dataclass(frozen=True)
class ChangedFile:
    status: str
    path: str


class EvidenceGenerator:
    def __init__(
        self,
        repository: GitRepository,
        github: GitHubData,
        *,
        log_path: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.github = github
        self.log_path = log_path or repository.common_git_dir() / "ao" / "evidence-events.jsonl"
        self.now = now or (lambda: datetime.now(UTC))

    def generate(
        self,
        pr_number: int,
        *,
        commit: bool = True,
        push: bool | None = None,
        ci_run_id: int | None = None,
    ) -> SnapshotResult | EvidenceSkip:
        should_push = commit if push is None else push
        if should_push and not commit:
            raise ValueError("Evidence cannot be pushed without a separate commit")
        started = time.monotonic()
        started_at = self.now()
        pull_request: PullRequest | None = None
        ci_run: CIRun | None = None
        try:
            self._require_clean_worktree()
            evidence_only = self._evidence_only_head()
            if evidence_only is not None:
                verified = verify_evidence_head(
                    self.repository,
                    self.github,
                    pr_number,
                    evidence_only.head_sha,
                    require_prior_attestation=True,
                )
                append_json_event(
                    self.log_path,
                    {
                        "action": "evidence.generate",
                        "ci_run_id": verified.ci_run_id,
                        "code_commit_sha": verified.code_commit_sha,
                        "duration_seconds": round(time.monotonic() - started, 6),
                        "finished_at": self.now().isoformat(),
                        "gate_status": "already-attested",
                        "head_sha": evidence_only.head_sha,
                        "pr_number": pr_number,
                        "reason": evidence_only.reason,
                        "result": "skipped",
                        "started_at": started_at.isoformat(),
                    },
                )
                return evidence_only
            pull_request = self.github.pull_request(pr_number)
            self._validate_pr_number(pr_number, pull_request)
            self.repository.fetch(
                "origin",
                pull_request.target_branch,
                pull_request.source_branch,
            )
            self._require_checked_out_code_commit(pull_request)
            ci_run = self.github.successful_ci_run(pull_request.head_sha, ci_run_id)
            self._validate_ci_binding(pull_request, ci_run)
            reviews = self.github.review_evidence(
                pull_request.number,
                pull_request.review_decision,
            )
            result = self._write_snapshot(pull_request, ci_run, reviews, started_at)
            evidence_commit = self._commit_snapshot(pull_request) if commit else None
            if should_push:
                self.repository.git(
                    "push",
                    "origin",
                    f"HEAD:{pull_request.source_branch}",
                )
            duration = round(time.monotonic() - started, 6)
            append_json_event(
                self.log_path,
                {
                    "action": "evidence.generate",
                    "ci_run_id": ci_run.run_id,
                    "code_commit_sha": pull_request.head_sha,
                    "duration_seconds": duration,
                    "evidence_commit_sha": evidence_commit,
                    "finished_at": self.now().isoformat(),
                    "gate_status": "ready-for-review",
                    "pr_number": pull_request.number,
                    "pushed": should_push,
                    "result": "success",
                    "started_at": started_at.isoformat(),
                },
            )
            return SnapshotResult(
                directory=result,
                code_commit_sha=pull_request.head_sha,
                ci_run_id=ci_run.run_id,
                evidence_commit_sha=evidence_commit,
            )
        except BaseException as error:
            duration = round(time.monotonic() - started, 6)
            append_json_event(
                self.log_path,
                {
                    "action": "evidence.generate",
                    "ci_run_id": ci_run.run_id if ci_run else None,
                    "code_commit_sha": pull_request.head_sha if pull_request else None,
                    "duration_seconds": duration,
                    "error": str(error),
                    "finished_at": self.now().isoformat(),
                    "gate_status": "indeterminate",
                    "pr_number": pr_number,
                    "push_requested": should_push,
                    "result": "failure",
                    "started_at": started_at.isoformat(),
                },
            )
            raise

    def _evidence_only_head(self) -> EvidenceSkip | None:
        head_sha = self.repository.git("rev-parse", "HEAD").stdout.strip()
        paths = tuple(
            path
            for path in self.repository.git(
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                "HEAD",
            ).stdout.splitlines()
            if path
        )
        if paths and all(path.startswith("docs/evidence/") for path in paths):
            return EvidenceSkip(
                head_sha=head_sha,
                reason=(
                    "HEAD is a fully verified, previously attested AO Evidence commit; "
                    "generation loop prevented"
                ),
            )
        return None

    def _require_clean_worktree(self) -> None:
        status = self.repository.git("status", "--porcelain").stdout
        if status:
            raise EvidenceError(
                "Evidence must be generated from a clean code commit; "
                "commit or remove working-tree changes first"
            )

    @staticmethod
    def _validate_pr_number(requested: int, pull_request: PullRequest) -> None:
        if pull_request.number != requested:
            raise EvidenceError(
                f"GitHub returned PR #{pull_request.number} for requested PR #{requested}"
            )

    def _require_checked_out_code_commit(self, pull_request: PullRequest) -> None:
        branch = self.repository.git("branch", "--show-current").stdout.strip()
        head = self.repository.git("rev-parse", "HEAD").stdout.strip()
        remote_head = self.repository.git(
            "rev-parse",
            f"origin/{pull_request.source_branch}",
        ).stdout.strip()
        if branch != pull_request.source_branch:
            raise EvidenceError(
                f"Evidence must be committed on PR branch {pull_request.source_branch}; "
                f"current branch is {branch or 'detached HEAD'}"
            )
        if head != pull_request.head_sha:
            raise EvidenceError(
                f"local HEAD {head} does not equal PR code commit {pull_request.head_sha}"
            )
        if remote_head != pull_request.head_sha:
            raise EvidenceError(
                f"remote PR branch moved to {remote_head} after GitHub metadata was read; "
                "restart generation for the new code commit"
            )

    @staticmethod
    def _validate_ci_binding(pull_request: PullRequest, ci_run: CIRun) -> None:
        if ci_run.commit_sha != pull_request.head_sha:
            raise EvidenceError(
                f"CI run {ci_run.run_id} belongs to {ci_run.commit_sha}, "
                f"not PR code commit {pull_request.head_sha}"
            )
        if (
            ci_run.status not in {"completed", "required-jobs-completed"}
            or ci_run.conclusion != "success"
        ):
            raise EvidenceError(
                f"CI run {ci_run.run_id} is not completed successfully; "
                "gate status is indeterminate"
            )

    def _write_snapshot(
        self,
        pull_request: PullRequest,
        ci_run: CIRun,
        reviews: ReviewEvidence,
        created_at: datetime,
    ) -> Path:
        base_ref = f"origin/{pull_request.target_branch}"
        comparison = f"{base_ref}...{pull_request.head_sha}"
        merge_base = self.repository.git(
            "merge-base",
            base_ref,
            pull_request.head_sha,
        ).stdout.strip()
        diff_args = (
            "diff",
            comparison,
            "--",
            ".",
            EVIDENCE_EXCLUSION,
        )
        patch = self.repository.git(*diff_args).stdout
        changed_files = _parse_name_status(
            self.repository.git(
                "diff",
                "--name-status",
                comparison,
                "--",
                ".",
                EVIDENCE_EXCLUSION,
            ).stdout
        )
        additions, deletions = _parse_numstat(
            self.repository.git(
                "diff",
                "--numstat",
                comparison,
                "--",
                ".",
                EVIDENCE_EXCLUSION,
            ).stdout
        )
        commits = _parse_commits(
            self.repository.git(
                "log",
                "--format=%H%x09%s",
                f"{merge_base}..{pull_request.head_sha}",
            ).stdout
        )
        contents = {
            "metadata.md": _metadata_markdown(
                pull_request,
                ci_run,
                merge_base,
                changed_files,
                additions,
                deletions,
                commits,
                created_at,
            ),
            "changed-files.md": _changed_files_markdown(
                pull_request,
                merge_base,
                changed_files,
                additions,
                deletions,
            ),
            "diff.patch": patch,
            "validation.md": _validation_markdown(
                pull_request,
                ci_run,
                reviews,
                merge_base,
                created_at,
            ),
            "advisor-context.md": _advisor_context_markdown(pull_request),
        }
        return _replace_evidence_directory(
            self.repository.root,
            pull_request.number,
            contents,
        )

    def _commit_snapshot(self, pull_request: PullRequest) -> str:
        relative = f"docs/evidence/pr-{pull_request.number}"
        self.repository.git("add", "-A", "--", relative)
        staged = tuple(
            line
            for line in self.repository.git(
                "diff",
                "--cached",
                "--name-only",
            ).stdout.splitlines()
            if line
        )
        if not staged:
            raise EvidenceError("Evidence generation produced no staged changes")
        prefix = f"{relative}/"
        if any(not path.startswith(prefix) for path in staged):
            raise EvidenceError(
                "refusing to commit because the index contains non-Evidence changes"
            )
        self.repository.git(
            "commit",
            "-m",
            f"docs: add PR{pull_request.number} review evidence",
        )
        return self.repository.git("rev-parse", "HEAD").stdout.strip()


def parse_evidence_binding(metadata_path: Path) -> EvidenceBinding:
    text = metadata_path.read_text(encoding="utf-8")
    values = {
        "pr_number": _metadata_value(text, "PR number"),
        "source_branch": _metadata_value(text, "Source branch"),
        "target_branch": _metadata_value(text, "Target branch"),
        "code_commit_sha": _metadata_value(text, "Code commit SHA"),
        "ci_run_id": _metadata_value(text, "CI run ID"),
        "ci_conclusion": _metadata_value(text, "CI conclusion"),
    }
    sha = values["code_commit_sha"]
    if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha):
        raise EvidenceError("metadata Code commit SHA must be a full lowercase SHA")
    try:
        pr_number = int(values["pr_number"].removeprefix("#"))
        ci_run_id = int(values["ci_run_id"])
    except ValueError as error:
        raise EvidenceError("metadata contains an invalid PR number or CI run ID") from error
    if values["ci_conclusion"] != "success":
        raise EvidenceError("metadata CI conclusion is not success")
    return EvidenceBinding(
        pr_number=pr_number,
        source_branch=values["source_branch"],
        target_branch=values["target_branch"],
        code_commit_sha=sha,
        ci_run_id=ci_run_id,
        ci_conclusion=values["ci_conclusion"],
    )


def verify_evidence_head(
    repository: GitRepository,
    github: GitHubData,
    pr_number: int,
    evidence_sha: str,
    *,
    require_prior_attestation: bool = False,
) -> VerifiedEvidenceHead:
    """Prove a head is an Evidence-only child of an exactly bound successful CI run."""
    _require_full_sha(evidence_sha, "Evidence commit SHA")
    head_sha = repository.git("rev-parse", "HEAD").stdout.strip()
    if head_sha != evidence_sha:
        raise EvidenceError(
            f"candidate HEAD {head_sha} does not equal Evidence commit {evidence_sha}"
        )

    relative = Path("docs") / "evidence" / f"pr-{pr_number}"
    directory = repository.root / relative
    if directory.is_symlink() or not directory.is_dir():
        raise EvidenceError("Evidence directory must be a real directory inside the repository")
    expected_paths = {str(relative / name) for name in EVIDENCE_FILES}
    actual_paths = {
        path
        for path in repository.git(
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            evidence_sha,
        ).stdout.splitlines()
        if path
    }
    if actual_paths != expected_paths:
        raise EvidenceError(
            "Evidence head must change exactly the five files under "
            f"{relative}; got {', '.join(sorted(actual_paths)) or 'no paths'}"
        )
    for name in EVIDENCE_FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise EvidenceError(f"Evidence entry must be a regular file: {path}")

    metadata_path = directory / "metadata.md"
    metadata_text = metadata_path.read_text(encoding="utf-8")
    binding = parse_evidence_binding(metadata_path)
    created_at = _metadata_created_at(metadata_text)
    if binding.pr_number != pr_number:
        raise EvidenceError(
            f"Evidence metadata binds PR #{binding.pr_number}, not requested PR #{pr_number}"
        )
    parent_sha = repository.git("rev-parse", f"{evidence_sha}^").stdout.strip()
    if parent_sha != binding.code_commit_sha:
        raise EvidenceError(
            f"Evidence parent {parent_sha} does not equal bound code commit "
            f"{binding.code_commit_sha}"
        )

    pull_request = github.pull_request(pr_number)
    if (
        pull_request.number != binding.pr_number
        or pull_request.source_branch != binding.source_branch
        or pull_request.target_branch != binding.target_branch
    ):
        raise EvidenceError("current PR identity or branches do not match Evidence metadata")
    if pull_request.head_sha != evidence_sha:
        raise EvidenceError(
            f"current PR head {pull_request.head_sha} does not equal Evidence commit {evidence_sha}"
        )
    repository.fetch("origin", pull_request.target_branch, pull_request.source_branch)
    remote_head = repository.git(
        "rev-parse",
        f"origin/{pull_request.source_branch}",
    ).stdout.strip()
    if remote_head != evidence_sha:
        raise EvidenceError(
            f"remote PR branch is {remote_head}, not verified Evidence commit {evidence_sha}"
        )

    ci_run = github.successful_ci_run(binding.code_commit_sha, binding.ci_run_id)
    if (
        ci_run.run_id != binding.ci_run_id
        or ci_run.commit_sha != binding.code_commit_sha
        or ci_run.status != "completed"
        or ci_run.conclusion != "success"
    ):
        raise EvidenceError("Evidence metadata does not bind an exact completed successful CI run")

    code_pull_request = replace(pull_request, head_sha=binding.code_commit_sha)
    base_ref = f"origin/{binding.target_branch}"
    comparison = f"{base_ref}...{binding.code_commit_sha}"
    merge_base = repository.git(
        "merge-base",
        base_ref,
        binding.code_commit_sha,
    ).stdout.strip()
    changed_files = _parse_name_status(
        repository.git(
            "diff",
            "--name-status",
            comparison,
            "--",
            ".",
            EVIDENCE_EXCLUSION,
        ).stdout
    )
    additions, deletions = _parse_numstat(
        repository.git(
            "diff",
            "--numstat",
            comparison,
            "--",
            ".",
            EVIDENCE_EXCLUSION,
        ).stdout
    )
    commits = _parse_commits(
        repository.git(
            "log",
            "--format=%H%x09%s",
            f"{merge_base}..{binding.code_commit_sha}",
        ).stdout
    )

    expected_metadata = _metadata_markdown(
        code_pull_request,
        ci_run,
        merge_base,
        changed_files,
        additions,
        deletions,
        commits,
        created_at,
    )
    if metadata_text != expected_metadata:
        raise EvidenceError(
            "Evidence metadata.md is incomplete or does not match the bound PR, "
            "commit, merge base, CI, and change metrics"
        )

    expected_changed_files = _changed_files_markdown(
        code_pull_request,
        merge_base,
        changed_files,
        additions,
        deletions,
    )
    actual_changed_files = (directory / "changed-files.md").read_text(encoding="utf-8")
    if actual_changed_files != expected_changed_files:
        raise EvidenceError(
            "Evidence changed-files.md does not match the recomputed three-dot "
            "name-status and line counts"
        )

    expected_patch = repository.git(
        "diff",
        comparison,
        "--",
        ".",
        EVIDENCE_EXCLUSION,
    ).stdout
    actual_patch = (directory / "diff.patch").read_text(encoding="utf-8")
    if actual_patch != expected_patch:
        raise EvidenceError("Evidence diff.patch does not match the bound three-dot code diff")

    validation_text = (directory / "validation.md").read_text(encoding="utf-8")
    recorded_reviews = _validation_review_evidence(validation_text, code_pull_request)
    current_reviews = github.review_evidence(
        pull_request.number,
        pull_request.review_decision,
    )
    if not set(recorded_reviews.line_comment_urls).issubset(
        current_reviews.line_comment_urls
    ) or not set(recorded_reviews.review_urls).issubset(current_reviews.review_urls):
        raise EvidenceError(
            "Evidence validation.md contains review URLs not present in GitHub's current PR records"
        )
    expected_validation = _validation_markdown(
        code_pull_request,
        ci_run,
        recorded_reviews,
        merge_base,
        created_at,
    )
    if validation_text != expected_validation:
        raise EvidenceError(
            "Evidence validation.md does not match the bound timestamp, CI run, "
            "required jobs, merge base, and generated review-record format"
        )

    expected_advisor_context = _advisor_context_markdown(code_pull_request)
    actual_advisor_context = (directory / "advisor-context.md").read_text(encoding="utf-8")
    if actual_advisor_context != expected_advisor_context:
        raise EvidenceError(
            "Evidence advisor-context.md does not match the bound PR, branch, and code commit"
        )

    if require_prior_attestation:
        try:
            attestation = github.evidence_attestation(
                evidence_sha,
                EVIDENCE_STATUS_CONTEXT,
            )
        except GitHubError as error:
            raise EvidenceError(
                "Evidence skip path lacks a prior trusted publisher attestation"
            ) from error
        expected_description = (
            f"Evidence-only child of {binding.code_commit_sha[:12]}; source CI {binding.ci_run_id}"
        )
        expected_target_prefix = f"https://github.com/{pull_request.repository}/actions/runs/"
        if (
            attestation.context != EVIDENCE_STATUS_CONTEXT
            or attestation.state != "success"
            or attestation.creator_id != 41898282
            or attestation.creator_login != "github-actions[bot]"
            or attestation.creator_type != "Bot"
            or attestation.description != expected_description
            or re.fullmatch(
                rf"{re.escape(expected_target_prefix)}[1-9][0-9]*",
                attestation.target_url,
            )
            is None
        ):
            raise EvidenceError(
                "Evidence skip path attestation does not match its parent, CI, "
                "publisher identity, and workflow provenance"
            )

    return VerifiedEvidenceHead(
        evidence_commit_sha=evidence_sha,
        code_commit_sha=binding.code_commit_sha,
        ci_run_id=binding.ci_run_id,
        ci_url=ci_run.url,
        prior_attestation=require_prior_attestation,
    )


def _require_full_sha(value: str, label: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise EvidenceError(f"{label} must be a full lowercase SHA")


def _metadata_value(text: str, label: str) -> str:
    match = re.search(
        rf"^\|\s*{re.escape(label)}\s*\|\s*`?([^|`]+?)`?\s*\|$",
        text,
        re.MULTILINE,
    )
    if not match:
        raise EvidenceError(f"metadata is missing required binding field {label}")
    return match.group(1).strip()


def _metadata_created_at(text: str) -> datetime:
    raw = _metadata_value(text, "Created at")
    try:
        created_at = datetime.fromisoformat(raw)
    except ValueError as error:
        raise EvidenceError("metadata Created at must be an ISO-8601 timestamp") from error
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise EvidenceError("metadata Created at must include a timezone")
    return created_at


def _validation_review_evidence(text: str, pr: PullRequest) -> ReviewEvidence:
    decision_match = re.search(
        r"^GitHub review decision at generation time: `([^`\r\n]+)`$",
        text,
        re.MULTILINE,
    )
    if decision_match is None:
        raise EvidenceError("validation.md is missing the generated review decision")
    comments = _validation_url_section(
        text,
        "Line-level comment URLs:",
        "Review record URLs:",
        f"{pr.url}#discussion_r",
    )
    reviews = _validation_url_section(
        text,
        "Review record URLs:",
        "Independent retrieval:",
        f"{pr.url}#pullrequestreview-",
    )
    return ReviewEvidence(
        decision=decision_match.group(1),
        line_comment_urls=comments,
        review_urls=reviews,
    )


def _validation_url_section(
    text: str,
    heading: str,
    next_heading: str,
    required_prefix: str,
) -> tuple[str, ...]:
    match = re.search(
        rf"^{re.escape(heading)}\n\n(?P<body>.*?)(?=\n\n^{re.escape(next_heading)}$)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise EvidenceError(f"validation.md is missing generated section {heading}")
    lines = tuple(line for line in match.group("body").splitlines() if line)
    if lines == ("- None",):
        return ()
    if not lines or any(not line.startswith("- ") for line in lines):
        raise EvidenceError(f"validation.md has malformed entries in {heading}")
    urls = tuple(line.removeprefix("- ") for line in lines)
    if any(not url.startswith(required_prefix) for url in urls):
        raise EvidenceError(f"validation.md has an out-of-scope URL in {heading}")
    if len(set(urls)) != len(urls):
        raise EvidenceError(f"validation.md has duplicate URLs in {heading}")
    return urls


def _replace_evidence_directory(
    repository_root: Path,
    pr_number: int,
    contents: dict[str, str],
) -> Path:
    if set(contents) != EVIDENCE_FILES:
        raise EvidenceError("Evidence Snapshot must contain exactly the five required files")
    docs = repository_root / "docs"
    parent = docs / "evidence"
    if docs.is_symlink() or parent.is_symlink():
        raise EvidenceError("Evidence parent directories must not be symbolic links")
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / f"pr-{pr_number}"
    if target.is_symlink():
        raise EvidenceError("Evidence target directory must not be a symbolic link")
    if target.exists():
        existing = {path.name for path in target.iterdir()}
        unexpected = existing - EVIDENCE_FILES
        if unexpected:
            raise EvidenceError(
                f"refusing to replace Evidence directory containing unexpected files: "
                f"{', '.join(sorted(unexpected))}"
            )
    temporary = Path(tempfile.mkdtemp(prefix=f".pr-{pr_number}-", dir=parent))
    backup = parent / f".pr-{pr_number}-backup"
    try:
        for name, content in contents.items():
            (temporary / name).write_text(content, encoding="utf-8")
        if backup.exists():
            raise EvidenceError(f"stale Evidence backup exists: {backup}")
        if target.exists():
            target.rename(backup)
        temporary.rename(target)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if target.exists() and backup.exists():
            shutil.rmtree(target)
        if backup.exists():
            backup.rename(target)
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return target


def _parse_name_status(output: str) -> tuple[ChangedFile, ...]:
    files: list[ChangedFile] = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        raw_status = fields[0]
        status = raw_status[0]
        path = " → ".join(fields[1:]) if status in {"R", "C"} else fields[-1]
        normalized = status if status in {"A", "D"} else "M"
        files.append(ChangedFile(normalized, path))
    return tuple(files)


def _parse_numstat(output: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in output.splitlines():
        fields = line.split("\t", maxsplit=2)
        if len(fields) < 3:
            continue
        if fields[0].isdigit():
            additions += int(fields[0])
        if fields[1].isdigit():
            deletions += int(fields[1])
    return additions, deletions


def _parse_commits(output: str) -> tuple[tuple[str, str], ...]:
    commits: list[tuple[str, str]] = []
    for line in output.splitlines():
        sha, separator, subject = line.partition("\t")
        if separator:
            commits.append((sha, subject))
    return tuple(commits)


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _metadata_markdown(
    pr: PullRequest,
    ci: CIRun,
    merge_base: str,
    files: tuple[ChangedFile, ...],
    additions: int,
    deletions: int,
    commits: tuple[tuple[str, str], ...],
    created_at: datetime,
) -> str:
    commit_rows = (
        "\n".join(f"| `{sha}` | {_escape(subject)} |" for sha, subject in commits)
        or "| — | No commits in comparison range |"
    )
    ci_timing_note = (
        "\n> The automatic Evidence job runs after the `quality`, `database`, and "
        "`container`\n> jobs succeed. At snapshot time the enclosing workflow is "
        "still active only because\n> this blocking Evidence job is its final job."
        if ci.status == "required-jobs-completed"
        else ""
    )
    return f"""# Evidence Snapshot for PR #{pr.number}

## Binding

| Field | Value |
|---|---|
| PR number | `#{pr.number}` |
| PR title | {_escape(pr.title)} |
| PR URL | {pr.url} |
| Source branch | `{pr.source_branch}` |
| Target branch | `{pr.target_branch}` |
| Code commit SHA | `{pr.head_sha}` |
| Merge base | `{merge_base}` |
| CI run ID | `{ci.run_id}` |
| CI conclusion | `{ci.conclusion}` |
| CI URL | {ci.url} |
| Created at | `{created_at.isoformat()}` |

> The CI result above belongs to code commit `{pr.head_sha}`, not to the later
> Evidence commit. The Evidence commit must not be used as a new generation trigger;
> doing so would create a CI/Evidence loop.
{ci_timing_note}

## Change overview

| Metric | Value |
|---|---:|
| Changed files | {len(files)} |
| Added lines | {additions} |
| Deleted lines | {deletions} |

The authoritative patch was generated with the three-dot comparison
`git diff origin/{pr.target_branch}...{pr.head_sha} -- . ':(exclude)docs/evidence/**'`.
The entire `docs/evidence/**` tree is excluded to prevent self-reference. No source
files are copied into this directory.

## Code commits

| Commit | Subject |
|---|---|
{commit_rows}

## Position and known limitations

This directory is a factual index and snapshot for PR #{pr.number}. It records a
fixed field set chosen by the tool's designers, so automatic generation does not
eliminate selection bias or blind spots. `diff.patch` is the complete authoritative
change record for scope questions, excluding only `docs/evidence/**`.

Evidence does not replace source review. An Advisor must read both this snapshot and
the source at code commit `{pr.head_sha}`. If Evidence and source disagree, source is
authoritative and the Evidence error must be reported. The tool cannot mechanically
prove that an Advisor actually read the source.
"""


def _changed_files_markdown(
    pr: PullRequest,
    merge_base: str,
    files: tuple[ChangedFile, ...],
    additions: int,
    deletions: int,
) -> str:
    labels = {"A": "Added", "M": "Modified", "D": "Deleted"}
    sections: list[str] = []
    for status in ("A", "M", "D"):
        matching = [item for item in files if item.status == status]
        rows = "\n".join(f"- `{_escape(item.path)}`" for item in matching) or "- None"
        sections.append(f"## {labels[status]} files\n\n{rows}")
    protected = " ".join(PROTECTED_PATHS)
    return f"""# Changed files for PR #{pr.number}

Comparison: `origin/{pr.target_branch}...{pr.head_sha}` (three-dot)

| Metric | Value |
|---|---:|
| Merge base | `{merge_base}` |
| Changed files | {len(files)} |
| Added lines | {additions} |
| Deleted lines | {deletions} |

{chr(10).join(sections)}

## Unmodified-boundary verification

Run the following command from any worktree for this repository. It must produce no
output; any output means a protected boundary changed:

```bash
git diff origin/{pr.target_branch}...{pr.head_sha} -- {protected}
```

The complete changed-path list can be reproduced without relying on this file:

```bash
git diff --name-status origin/{pr.target_branch}...{pr.head_sha} -- . ':(exclude)docs/evidence/**'
```
"""


def _validation_markdown(
    pr: PullRequest,
    ci: CIRun,
    reviews: ReviewEvidence,
    merge_base: str,
    created_at: datetime,
) -> str:
    job_rows = "\n".join(
        f"| {_escape(job.name)} | `{job.conclusion}` | {job.url} |"
        for job in sorted(ci.jobs, key=lambda item: item.name)
    )
    comment_rows = "\n".join(f"- {url}" for url in reviews.line_comment_urls) or "- None"
    review_rows = "\n".join(f"- {url}" for url in reviews.review_urls) or "- None"
    ci_timing_note = (
        "\nThe automatic Evidence job observed all three required code jobs as "
        "successful. The\nworkflow status was still active solely because the "
        "blocking Evidence job had not yet\nfinished."
        if ci.status == "required-jobs-completed"
        else ""
    )
    return f"""# Validation record for PR #{pr.number}

Created at: `{created_at.isoformat()}`

This file records evidence locations and results, not an evaluation of their
correctness. When a record disagrees with its original source, the original source
is authoritative.

## CI status

| Field | Value |
|---|---|
| Run ID | `{ci.run_id}` |
| Code commit | `{ci.commit_sha}` |
| Status | `{ci.status}` |
| Conclusion | `{ci.conclusion}` |
| URL | {ci.url} |
| Merge base | `{merge_base}` |

> This CI result applies to code commit `{pr.head_sha}`, not to the Evidence commit
> that contains this directory. Do not regenerate Evidence to describe its own
> commit. A later code change requires a new successful CI run and a regenerated
> snapshot.
{ci_timing_note}

| Job | Conclusion | URL |
|---|---|---|
{job_rows}

CI retrieval:

```bash
gh run view {ci.run_id} --json jobs
gh run view {ci.run_id} --log
gh run download {ci.run_id} --name quality-reports
gh run download {ci.run_id} --name database-reports
gh run download {ci.run_id} --name clean-start-evidence
```

## Test evidence

Test result locations are the `quality-reports` and `database-reports` artifacts
from run `{ci.run_id}`. Reproduce the repository checks with:

```bash
make build-reproducible
make typecheck
make lint
make test-unit
make test-integration
```

## Reviewer status

GitHub review decision at generation time: `{_escape(reviews.decision)}`

Line-level comment URLs:

{comment_rows}

Review record URLs:

{review_rows}

Independent retrieval:

```bash
gh api repos/{pr.repository}/pulls/{pr.number}/comments
gh api repos/{pr.repository}/pulls/{pr.number}/reviews
```
"""


def _advisor_context_markdown(pr: PullRequest) -> str:
    return f"""# Advisor context for PR #{pr.number}

## Required review order

1. Read `metadata.md` and verify the PR, branch, code commit, and CI binding.
2. Run the boundary command in `changed-files.md`.
3. Read `validation.md` and follow its original evidence links.
4. Inspect `diff.patch` for the complete three-dot change record.
5. Use the Advisor Workspace Resolver to inspect the source at code commit
   `{pr.head_sha}` in detached HEAD state.

The original Evidence files are in `docs/evidence/pr-{pr.number}/` on branch
`{pr.source_branch}`. The source of record is the repository at code commit
`{pr.head_sha}`.

## Non-substitution rule

This directory cannot replace source judgment. Evidence and the corresponding
source commit are both required. If they disagree, source is authoritative and the
Evidence error must be called out in the Advisor opinion. An opinion must record the
actual reviewed commit SHA so that the gate can invalidate it after a later code
change.
"""
