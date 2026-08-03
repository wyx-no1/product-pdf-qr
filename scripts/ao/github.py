from __future__ import annotations

import json
from typing import Protocol, cast

from scripts.ao.git import CommandRunner
from scripts.ao.models import (
    CIJob,
    CIRun,
    EvidenceAttestation,
    PullRequest,
    ReviewEvidence,
)

REQUIRED_CI_JOBS = {"quality", "database", "container"}


class GitHubData(Protocol):
    def pull_request(self, number: int) -> PullRequest: ...

    def successful_ci_run(
        self,
        commit_sha: str,
        run_id: int | None = None,
    ) -> CIRun: ...

    def review_evidence(self, number: int, decision: str) -> ReviewEvidence: ...

    def evidence_attestation(
        self,
        commit_sha: str,
        context: str,
    ) -> EvidenceAttestation: ...


class GitHubError(RuntimeError):
    """Required GitHub evidence is missing or not successful."""


class CINotRunError(GitHubError):
    """A CI record exists, but its required jobs did not execute."""


class CIFailedError(GitHubError):
    """CI executed and reached a non-successful conclusion."""


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

    def successful_ci_run(
        self,
        commit_sha: str,
        run_id: int | None = None,
    ) -> CIRun:
        if run_id is not None:
            return self._successful_current_run(commit_sha, run_id)
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
        run_id = _required_int(latest, "databaseId")
        return self._validated_run(latest, run_id)

    def _successful_current_run(self, commit_sha: str, run_id: int) -> CIRun:
        raw = self._json(
            "run",
            "view",
            str(run_id),
            "--repo",
            self.repository,
            "--json",
            "headSha,status,conclusion,url,jobs",
        )
        data = cast(dict[str, object], raw)
        if _required_sha(data, "headSha") != commit_sha:
            raise GitHubError(f"CI run {run_id} does not belong to code commit {commit_sha}")
        return self._validated_run(data, run_id, allow_active=True)

    def _validated_run(
        self,
        data: dict[str, object],
        run_id: int,
        *,
        allow_active: bool = False,
    ) -> CIRun:
        status = _required_str(data, "status")
        conclusion = str(data.get("conclusion") or "")
        jobs_value = data.get("jobs")
        if not isinstance(jobs_value, list):
            jobs_raw = self._json(
                "run",
                "view",
                str(run_id),
                "--repo",
                self.repository,
                "--json",
                "jobs",
            )
            jobs_value = cast(dict[str, object], jobs_raw).get("jobs")
        if not isinstance(jobs_value, list):
            raise GitHubError(f"CI run {run_id} did not return jobs")
        jobs_data = cast(list[dict[str, object]], jobs_value)
        jobs = tuple(
            CIJob(
                name=_required_str(job, "name"),
                conclusion=str(job.get("conclusion") or ""),
                url=_required_str(job, "url"),
            )
            for job in jobs_data
        )
        if conclusion == "action_required" or not jobs:
            raise CINotRunError(
                f"CI run {run_id} did not execute required jobs "
                f"(status={status}, conclusion={conclusion or 'none'}, jobs={len(jobs)}); "
                "this is not a success or a test failure, so gate status is indeterminate"
            )
        if not allow_active and status != "completed":
            raise GitHubError(
                f"CI run {run_id} is not completed "
                f"(status={status}, conclusion={conclusion or 'none'}); "
                "gate status is indeterminate"
            )
        if allow_active and status not in {"in_progress", "completed"}:
            raise GitHubError(
                f"CI run {run_id} is not active or completed "
                f"(status={status}, conclusion={conclusion or 'none'}); "
                "gate status is indeterminate"
            )
        if status == "completed" and conclusion != "success":
            raise CIFailedError(
                f"CI run {run_id} executed but failed "
                f"(status={status}, conclusion={conclusion or 'none'})"
            )
        required_jobs = tuple(job for job in jobs if job.name in REQUIRED_CI_JOBS)
        unsuccessful = [job.name for job in required_jobs if job.conclusion != "success"]
        missing = REQUIRED_CI_JOBS - {job.name for job in required_jobs}
        if not required_jobs or unsuccessful or missing:
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
            commit_sha=_required_sha(data, "headSha"),
            status="required-jobs-completed" if status == "in_progress" else status,
            conclusion="success",
            url=_required_str(data, "url"),
            jobs=required_jobs,
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

    def evidence_attestation(
        self,
        commit_sha: str,
        context: str,
    ) -> EvidenceAttestation:
        raw = self._json(
            "api",
            f"repos/{self.repository}/commits/{commit_sha}/status",
        )
        data = cast(dict[str, object], raw)
        statuses_value = data.get("statuses")
        if not isinstance(statuses_value, list):
            raise GitHubError(f"commit {commit_sha} did not return status records")
        statuses = cast(list[dict[str, object]], statuses_value)
        status = next(
            (item for item in statuses if item.get("context") == context),
            None,
        )
        if status is None:
            raise GitHubError(f"commit {commit_sha} has no prior trusted {context} attestation")
        creator = status.get("creator")
        creator_login = (
            str(cast(dict[str, object], creator).get("login") or "")
            if isinstance(creator, dict)
            else ""
        )
        return EvidenceAttestation(
            context=_required_str(status, "context"),
            state=_required_str(status, "state"),
            creator_login=creator_login,
            description=_required_str(status, "description"),
            target_url=_required_str(status, "target_url"),
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
