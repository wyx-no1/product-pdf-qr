from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.ao.models import CIJob, CIRun, PullRequest, ReviewEvidence


def git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=check,
        capture_output=True,
        text=True,
    )


@dataclass(frozen=True)
class GitFixture:
    repository: Path
    origin: Path
    base_sha: str
    code_sha: str


def make_diverged_repository(tmp_path: Path) -> GitFixture:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    git(tmp_path, "init", "--bare", str(origin))
    git(tmp_path, "init", "-b", "main", str(repository))
    git(repository, "config", "user.name", "AO Test")
    git(repository, "config", "user.email", "ao@example.invalid")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    git(repository, "add", "base.txt")
    git(repository, "commit", "-m", "base")
    base_sha = git(repository, "rev-parse", "HEAD").stdout.strip()
    git(repository, "remote", "add", "origin", str(origin))
    git(repository, "push", "-u", "origin", "main")

    git(repository, "switch", "-c", "feature/evidence")
    (repository / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(repository, "add", "feature.txt")
    git(repository, "commit", "-m", "feat: add feature")
    code_sha = git(repository, "rev-parse", "HEAD").stdout.strip()
    git(repository, "push", "-u", "origin", "feature/evidence")

    git(repository, "switch", "main")
    (repository / "main-only.txt").write_text("main only\n", encoding="utf-8")
    git(repository, "add", "main-only.txt")
    git(repository, "commit", "-m", "docs: advance main")
    git(repository, "push", "origin", "main")
    git(repository, "switch", "feature/evidence")
    return GitFixture(repository, origin, base_sha, code_sha)


class FakeGitHubData:
    def __init__(
        self,
        pull_request: PullRequest,
        ci_run: CIRun,
        reviews: ReviewEvidence | None = None,
    ) -> None:
        self.pr = pull_request
        self.ci = ci_run
        self.reviews = reviews or ReviewEvidence("not-reviewed", (), ())
        self.calls: list[str] = []

    def pull_request(self, number: int) -> PullRequest:
        self.calls.append("pull_request")
        assert number == self.pr.number
        return self.pr

    def successful_ci_run(
        self,
        commit_sha: str,
        run_id: int | None = None,
    ) -> CIRun:
        self.calls.append("successful_ci_run")
        assert commit_sha == self.pr.head_sha
        if run_id is not None:
            assert run_id == self.ci.run_id
        return self.ci

    def review_evidence(self, number: int, decision: str) -> ReviewEvidence:
        self.calls.append("review_evidence")
        assert number == self.pr.number
        assert decision == self.pr.review_decision
        return self.reviews


def github_for(code_sha: str, *, number: int = 41) -> FakeGitHubData:
    pull_request = PullRequest(
        number=number,
        repository="example/repository",
        title="Synthetic evidence",
        source_branch="feature/evidence",
        target_branch="main",
        head_sha=code_sha,
        url=f"https://example.invalid/pull/{number}",
        review_decision="not-reviewed",
    )
    jobs = tuple(
        CIJob(name, "success", f"https://example.invalid/jobs/{name}")
        for name in ("quality", "database", "container")
    )
    ci_run = CIRun(
        run_id=9001,
        commit_sha=code_sha,
        status="completed",
        conclusion="success",
        url="https://example.invalid/actions/9001",
        jobs=jobs,
    )
    return FakeGitHubData(pull_request, ci_run)


def write_metadata(path: Path, code_sha: str, *, number: int = 41) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""# Evidence

| Field | Value |
|---|---|
| PR number | `#{number}` |
| Source branch | `feature/evidence` |
| Target branch | `main` |
| Code commit SHA | `{code_sha}` |
| CI run ID | `9001` |
| CI conclusion | `success` |
""",
        encoding="utf-8",
    )
    return path


def commit_paths(repository: Path, message: str, paths: Sequence[str]) -> str:
    git(repository, "add", "--", *paths)
    git(repository, "commit", "-m", message)
    return git(repository, "rev-parse", "HEAD").stdout.strip()
