from __future__ import annotations

import json
from typing import Protocol, cast

from scripts.ao.git import CommandRunner
from scripts.ao.models import CIJob, CIRun, PullRequest, ReviewEvidence

REQUIRED_CI_JOBS = {"quality", "database", "container"}


class GitHubData(Protocol):
    def pull_request(self, number: int) -> PullRequest: ...

    def successful_ci_run(self, commit_sha: str) -> CIRun: ...

    def review_evidence(self, number: int, decision: str) -> ReviewEvidence: ...


class GitHubError(RuntimeError):
    """Required GitHub evidence is missing or not successful."""


class GhGitHubData:
    def __init__(
        self,
        repository: str,
        runner: CommandRunner | None = None,
        workflow: str = "CI",
    ) -> None:
        self.repository = repository
        self.runner = runner or CommandRunner()
        self.workflow = workflow

    def _json(self, *args: str) -> object:
        result = self.runner.run(("gh", *args))
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubError(f"gh returned invalid JSON for {' '.join(args)}") from error

    def pull_request(self, number: int) -> PullRequest:
        fields = "number,title,headRefName,baseRefName,headRefOid,url,reviewDecision"
        raw = self._json(
            "pr",
            "view",
            str(number),
            "--repo",
            self.repository,
            "--json",
            fields,
        )
        data = cast(dict[str, object], raw)
        return PullRequest(
            number=_required_int(data, "number"),
            repository=self.repository,
            title=_required_str(data, "title"),
            source_branch=_required_str(data, "headRefName"),
            target_branch=_required_str(data, "baseRefName"),
            head_sha=_required_sha(data, "headRefOid"),
            url=_required_str(data, "url"),
            review_decision=str(data.get("reviewDecision") or "not-reviewed"),
        )

    def successful_ci_run(self, commit_sha: str) -> CIRun:
        raw = self._json(
            "run",
            "list",
            "--repo",
            self.repository,
            "--workflow",
            self.workflow,
            "--commit",
            commit_sha,
            "--limit",
            "100",
            "--json",
            "databaseId,headSha,status,conclusion,url,createdAt",
        )
        runs = cast(list[dict[str, object]], raw)
        matching = [run for run in runs if run.get("headSha") == commit_sha]
        if not matching:
            raise GitHubError(f"no {self.workflow} CI run found for code commit {commit_sha}")
        latest = max(matching, key=lambda run: str(run.get("createdAt") or ""))
        status = _required_str(latest, "status")
        conclusion = str(latest.get("conclusion") or "")
        if status != "completed" or conclusion != "success":
            raise GitHubError(
                f"CI for code commit {commit_sha} is not successful "
                f"(status={status}, conclusion={conclusion or 'none'}); "
                "gate status is indeterminate and Evidence must not be generated"
            )
        run_id = _required_int(latest, "databaseId")
        jobs_raw = self._json(
            "run",
            "view",
            str(run_id),
            "--repo",
            self.repository,
            "--json",
            "jobs",
        )
        jobs_data = cast(dict[str, list[dict[str, object]]], jobs_raw).get("jobs", [])
        jobs = tuple(
            CIJob(
                name=_required_str(job, "name"),
                conclusion=str(job.get("conclusion") or ""),
                url=_required_str(job, "url"),
            )
            for job in jobs_data
        )
        unsuccessful = [job.name for job in jobs if job.conclusion != "success"]
        missing = REQUIRED_CI_JOBS - {job.name for job in jobs}
        if not jobs or unsuccessful or missing:
            details: list[str] = []
            if unsuccessful:
                details.append(f"unsuccessful: {', '.join(unsuccessful)}")
            if missing:
                details.append(f"missing: {', '.join(sorted(missing))}")
            detail = "; ".join(details) if details else "no jobs returned"
            raise GitHubError(
                f"CI run {run_id} lacks an all-successful job set ({detail}); "
                "Evidence must not be generated"
            )
        return CIRun(
            run_id=run_id,
            commit_sha=_required_sha(latest, "headSha"),
            status=status,
            conclusion=conclusion,
            url=_required_str(latest, "url"),
            jobs=jobs,
        )

    def review_evidence(self, number: int, decision: str) -> ReviewEvidence:
        comments_raw = self._json(
            "api",
            f"repos/{self.repository}/pulls/{number}/comments",
        )
        reviews_raw = self._json(
            "api",
            f"repos/{self.repository}/pulls/{number}/reviews",
        )
        comments = cast(list[dict[str, object]], comments_raw)
        reviews = cast(list[dict[str, object]], reviews_raw)
        return ReviewEvidence(
            decision=decision,
            line_comment_urls=tuple(
                url for item in comments if (url := str(item.get("html_url") or ""))
            ),
            review_urls=tuple(url for item in reviews if (url := str(item.get("html_url") or ""))),
        )


def _required_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GitHubError(f"GitHub response is missing required field {key}")
    return value


def _required_int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise GitHubError(f"GitHub response is missing required integer field {key}")
    return value


def _required_sha(data: dict[str, object], key: str) -> str:
    value = _required_str(data, key)
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise GitHubError(f"GitHub field {key} is not a full lowercase commit SHA")
    return value
