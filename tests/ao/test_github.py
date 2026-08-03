from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts.ao.git import CommandResult, CommandRunner
from scripts.ao.github import CIFailedError, CINotRunError, GhGitHubData, GitHubError

SHA = "a" * 40


class StubRunner(CommandRunner):
    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = [json.dumps(response) for response in responses]
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> CommandResult:
        del cwd, check
        self.calls.append(tuple(args))
        return CommandResult(self.responses.pop(0), "", 0)


def test_gh_provider_accepts_only_exact_successful_three_job_run() -> None:
    runner = StubRunner(
        [
            [
                {
                    "conclusion": "success",
                    "createdAt": "2026-08-03T12:00:00Z",
                    "databaseId": 77,
                    "headSha": SHA,
                    "status": "completed",
                    "url": "https://example.invalid/runs/77",
                }
            ],
            {
                "jobs": [
                    {
                        "conclusion": "success",
                        "name": name,
                        "url": f"https://example.invalid/jobs/{name}",
                    }
                    for name in ("quality", "database", "container")
                ]
            },
        ]
    )

    ci_run = GhGitHubData("owner/repository", runner=runner).successful_ci_run(SHA)

    assert ci_run.run_id == 77
    assert ci_run.commit_sha == SHA
    assert [job.name for job in ci_run.jobs] == ["quality", "database", "container"]
    assert all(call[0] == "gh" for call in runner.calls)


def test_gh_provider_rejects_missing_required_job_without_network() -> None:
    runner = StubRunner(
        [
            [
                {
                    "conclusion": "success",
                    "createdAt": "2026-08-03T12:00:00Z",
                    "databaseId": 77,
                    "headSha": SHA,
                    "status": "completed",
                    "url": "https://example.invalid/runs/77",
                }
            ],
            {
                "jobs": [
                    {
                        "conclusion": "success",
                        "name": name,
                        "url": f"https://example.invalid/jobs/{name}",
                    }
                    for name in ("quality", "database")
                ]
            },
        ]
    )

    with pytest.raises(GitHubError, match="missing: container"):
        GhGitHubData("owner/repository", runner=runner).successful_ci_run(SHA)


def test_gh_provider_accepts_current_run_after_required_jobs_complete() -> None:
    runner = StubRunner(
        [
            {
                "conclusion": "",
                "headSha": SHA,
                "jobs": [
                    {
                        "conclusion": "success",
                        "name": name,
                        "url": f"https://example.invalid/jobs/{name}",
                    }
                    for name in ("quality", "database", "container")
                ]
                + [
                    {
                        "conclusion": "",
                        "name": "evidence",
                        "url": "https://example.invalid/jobs/evidence",
                    }
                ],
                "status": "in_progress",
                "url": "https://example.invalid/runs/88",
            }
        ]
    )

    ci_run = GhGitHubData("owner/repository", runner=runner).successful_ci_run(SHA, 88)

    assert ci_run.run_id == 88
    assert ci_run.status == "required-jobs-completed"
    assert ci_run.conclusion == "success"
    assert {job.name for job in ci_run.jobs} == {"quality", "database", "container"}


def test_action_required_zero_job_run_is_indeterminate_not_failed_or_successful() -> None:
    runner = StubRunner(
        [
            [
                {
                    "conclusion": "action_required",
                    "createdAt": "2026-08-03T12:00:00Z",
                    "databaseId": 99,
                    "headSha": SHA,
                    "status": "completed",
                    "url": "https://example.invalid/runs/99",
                }
            ],
            {"jobs": []},
        ]
    )

    with pytest.raises(CINotRunError, match="not a success or a test failure"):
        GhGitHubData("owner/repository", runner=runner).successful_ci_run(SHA)


def test_executed_failed_run_is_distinct_from_not_run() -> None:
    jobs = [
        {
            "conclusion": "failure" if name == "quality" else "success",
            "name": name,
            "url": f"https://example.invalid/jobs/{name}",
        }
        for name in ("quality", "database", "container")
    ]
    runner = StubRunner(
        [
            [
                {
                    "conclusion": "failure",
                    "createdAt": "2026-08-03T12:00:00Z",
                    "databaseId": 100,
                    "headSha": SHA,
                    "status": "completed",
                    "url": "https://example.invalid/runs/100",
                }
            ],
            {"jobs": jobs},
        ]
    )

    with pytest.raises(CIFailedError, match="executed but failed"):
        GhGitHubData("owner/repository", runner=runner).successful_ci_run(SHA)
