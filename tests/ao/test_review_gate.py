from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.ao.git import CommandFailed, CommandResult, CommandRunner
from scripts.ao.models import PullRequest
from scripts.ao.review_gate import (
    CIObservation,
    GhReviewGateData,
    ReviewCommit,
    ReviewGateError,
    ReviewRecord,
    _latest_observations,
    evaluate_review_gate,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
BASE_SHA = "a" * 40
CODE_SHA = "b" * 40
METADATA_SHA = "d" * 40
TRUSTED_REVIEWER_ID = 306825498


class FakeReviewGateData:
    def __init__(
        self,
        commits: tuple[ReviewCommit, ...],
        reviews: tuple[ReviewRecord, ...] = (),
        *,
        failed_jobs: frozenset[str] = frozenset(),
    ) -> None:
        self.commits = commits
        self.reviews = reviews
        self.failed_jobs = failed_jobs

    def repository_owner_id(self) -> int:
        return TRUSTED_REVIEWER_ID

    def pull_request(self, number: int) -> PullRequest:
        return PullRequest(
            number=number,
            repository="owner/repository",
            title="Review gate",
            source_branch="feature/review-gate",
            target_branch="main",
            head_sha=self.commits[-1].sha,
            url=f"https://example.invalid/pull/{number}",
            review_decision="not-reviewed",
        )

    def pull_request_commits(self, number: int) -> tuple[ReviewCommit, ...]:
        assert number == 19
        return self.commits

    def pull_request_reviews(self, number: int) -> tuple[ReviewRecord, ...]:
        assert number == 19
        return self.reviews

    def commit_ci(self, commit_sha: str) -> dict[str, CIObservation]:
        assert commit_sha in {commit.sha for commit in self.commits}
        return {
            name: CIObservation(
                name=name,
                state="failure" if name in self.failed_jobs else "success",
                source="check",
                observed_at=NOW,
                observation_id=index,
            )
            for index, name in enumerate(("quality", "database", "container"), start=1)
        }


def test_code_commit_without_any_review_is_a_gap() -> None:
    github = FakeReviewGateData((ReviewCommit(CODE_SHA, ("scripts/ao/cli.py",)),))

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "REVIEW_GAP"
    assert result.exit_code != 0
    assert "missing anchored review" in result.render()


def test_review_anchored_to_earlier_sha_does_not_cover_new_commit() -> None:
    github = FakeReviewGateData(
        (
            ReviewCommit(BASE_SHA, ("scripts/ao/cli.py",)),
            ReviewCommit(CODE_SHA, ("scripts/ao/review_gate.py",)),
        ),
        (_review(BASE_SHA, "approved"),),
    )

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    code_result = result.commits[1]
    assert code_result.verdict == "missing"
    assert "missing anchored review" in code_result.gaps[0]


def test_code_commit_with_failed_ci_is_a_gap() -> None:
    github = FakeReviewGateData(
        (ReviewCommit(CODE_SHA, ("scripts/ao/review_gate.py",)),),
        (_review(CODE_SHA, "approved"),),
        failed_jobs=frozenset({"quality"}),
    )

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "REVIEW_GAP"
    assert "required CI jobs not successful: quality=failure" in result.commits[0].gaps


def test_newer_running_check_does_not_eclipse_completed_success() -> None:
    observations = (
        _ci_observation("success", observed_at=NOW, observation_id=1),
        _ci_observation(
            "in_progress",
            observed_at=datetime(2026, 8, 5, 12, 1, tzinfo=UTC),
            observation_id=2,
        ),
    )

    selected = _latest_observations(observations)

    assert selected["quality"].state == "success"


def test_newer_completed_failure_supersedes_completed_success() -> None:
    observations = (
        _ci_observation("success", observed_at=NOW, observation_id=1),
        _ci_observation(
            "failure",
            observed_at=datetime(2026, 8, 5, 12, 1, tzinfo=UTC),
            observation_id=2,
        ),
    )

    selected = _latest_observations(observations)

    assert selected["quality"].state == "failure"


def test_running_check_remains_a_gap_without_completed_observation() -> None:
    selected = _latest_observations(
        (
            _ci_observation(
                "in_progress",
                observed_at=datetime(2026, 8, 5, 12, 1, tzinfo=UTC),
                observation_id=2,
            ),
        )
    )

    assert selected["quality"].state == "in_progress"


def test_metadata_only_commit_is_exempt_from_ci_and_review() -> None:
    github = FakeReviewGateData(
        (
            ReviewCommit(
                METADATA_SHA,
                ("docs/evidence/pr-19/metadata.md", "docs/evidence/pr-19/validation.md"),
            ),
        )
    )

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "PASS"
    assert result.exit_code == 0
    assert result.commits[0].classification == "METADATA"
    assert result.commits[0].ci_status == "exempt"


def test_all_compliant_commits_pass_with_exit_code_zero() -> None:
    github = FakeReviewGateData(
        (
            ReviewCommit(BASE_SHA, ("scripts/ao/cli.py",)),
            ReviewCommit(CODE_SHA, ("scripts/ao/review_gate.py",)),
            ReviewCommit(METADATA_SHA, ("docs/evidence/pr-19/metadata.md",)),
        ),
        (
            _review(BASE_SHA, "changes requested", review_id=1),
            _review(CODE_SHA, "approved", review_id=2),
        ),
    )

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "PASS"
    assert result.exit_code == 0
    assert "METADATA" in result.render()


def test_unparseable_verdict_fails_closed_and_reports_stalled() -> None:
    github = FakeReviewGateData(
        (ReviewCommit(CODE_SHA, ("scripts/ao/review_gate.py",)),),
        (
            ReviewRecord(
                review_id=1,
                commit_id=CODE_SHA,
                state="COMMENTED",
                body="Looks reasonable, but this is not a machine verdict.",
                submitted_at=datetime(2026, 8, 5, 11, 0, tzinfo=UTC),
                url="https://example.invalid/review/1",
                author_id=TRUSTED_REVIEWER_ID,
                author_login="trusted-reviewer",
                author_type="User",
                author_association="OWNER",
            ),
        ),
    )

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "REVIEW_GAP"
    assert result.commits[0].verdict == "indeterminate"
    assert result.commits[0].stalled is True
    assert "STALLED" in result.render()


def test_untrusted_reviewer_cannot_supply_an_approved_verdict() -> None:
    github = FakeReviewGateData(
        (ReviewCommit(CODE_SHA, ("scripts/ao/review_gate.py",)),),
        (_review(CODE_SHA, "approved", author_id=999),),
    )

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "REVIEW_GAP"
    assert result.commits[0].verdict == "untrusted"
    assert "not from a trusted reviewer" in result.commits[0].gaps[0]


def test_chinese_approved_heading_passes_the_gate() -> None:
    review = replace(
        _review(CODE_SHA, "approved"),
        body="## 审查结论：通过\n\n可复核的审查依据。",  # noqa: RUF001
    )
    github = FakeReviewGateData(
        (ReviewCommit(CODE_SHA, ("scripts/ao/review_gate.py",)),),
        (review,),
    )

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "PASS"
    assert result.exit_code == 0
    assert result.commits[0].verdict == "approved"


def test_chinese_changes_requested_heading_is_recognized() -> None:
    review = replace(
        _review(CODE_SHA, "approved"),
        body="## 审查结论：需要修改\n\n发现需要修改的问题。",  # noqa: RUF001
    )
    github = FakeReviewGateData(
        (ReviewCommit(CODE_SHA, ("scripts/ao/review_gate.py",)),),
        (review,),
    )

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "REVIEW_GAP"
    assert result.commits[0].verdict == "changes_requested"
    assert "final code commit verdict must be approved" in result.commits[0].gaps[0]


def test_agreeing_english_and_chinese_headings_are_ambiguous() -> None:
    review = replace(
        _review(CODE_SHA, "approved"),
        body="## Review verdict: approved\n\n## 审查结论：通过\n\nSame meaning twice.",  # noqa: RUF001
    )
    github = FakeReviewGateData(
        (ReviewCommit(CODE_SHA, ("scripts/ao/review_gate.py",)),),
        (review,),
    )

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "REVIEW_GAP"
    assert result.commits[0].verdict == "indeterminate"


def test_duplicate_chinese_headings_are_ambiguous() -> None:
    review = replace(
        _review(CODE_SHA, "approved"),
        body="## 审查结论：通过\n\n## 审查结论：需要修改\n\n互相矛盾的结论。",  # noqa: RUF001
    )
    github = FakeReviewGateData(
        (ReviewCommit(CODE_SHA, ("scripts/ao/review_gate.py",)),),
        (review,),
    )

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "REVIEW_GAP"
    assert result.commits[0].verdict == "indeterminate"


@pytest.mark.parametrize(
    "body",
    [
        "## 审查结论：通过（Ready）\n\n结论带装饰后缀。",  # noqa: RUF001
        "## Review verdict: 通过\n\n跨语言混拼的结论值。",
        "## 审查结论：approved\n\n跨语言混拼的结论值。",  # noqa: RUF001
    ],
)
def test_decorated_or_cross_language_verdict_values_fail_closed(body: str) -> None:
    review = replace(_review(CODE_SHA, "approved"), body=body)
    github = FakeReviewGateData(
        (ReviewCommit(CODE_SHA, ("scripts/ao/review_gate.py",)),),
        (review,),
    )

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "REVIEW_GAP"
    assert result.commits[0].verdict == "indeterminate"


def test_known_and_unknown_verdict_headings_are_ambiguous() -> None:
    review = _review(CODE_SHA, "approved")
    ambiguous = replace(
        review,
        body=(
            "## Review verdict: approved\n\n"
            "## Review verdict: rejected\n\n"
            "Conflicting machine headings."
        ),
    )
    github = FakeReviewGateData(
        (ReviewCommit(CODE_SHA, ("scripts/ao/review_gate.py",)),),
        (ambiguous,),
    )

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "REVIEW_GAP"
    assert result.commits[0].verdict == "indeterminate"


class FailingRunner(CommandRunner):
    def __init__(self, error: Exception) -> None:
        self.error = error

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> CommandResult:
        del args, cwd, check
        raise self.error


def test_missing_gh_fails_explicitly_instead_of_passing() -> None:
    github = GhReviewGateData("owner/repository", runner=FailingRunner(FileNotFoundError()))

    with pytest.raises(ReviewGateError, match="required command 'gh' is unavailable"):
        github.pull_request(19)


@pytest.mark.parametrize("detail", ["network unreachable", "API returned 502"])
def test_network_or_api_failure_fails_explicitly(detail: str) -> None:
    error = CommandFailed(("gh", "api", "endpoint"), 1, detail)
    github = GhReviewGateData("owner/repository", runner=FailingRunner(error))

    with pytest.raises(ReviewGateError, match=detail):
        github.pull_request(19)


def _review(
    sha: str,
    verdict: str,
    *,
    review_id: int = 1,
    author_id: int = TRUSTED_REVIEWER_ID,
) -> ReviewRecord:
    return ReviewRecord(
        review_id=review_id,
        commit_id=sha,
        state="COMMENTED",
        body=f"## Review verdict: {verdict}\n\nAuditable details.",
        submitted_at=NOW,
        url=f"https://example.invalid/review/{review_id}",
        author_id=author_id,
        author_login="trusted-reviewer" if author_id == TRUSTED_REVIEWER_ID else "attacker",
        author_type="User",
        author_association="OWNER" if author_id == TRUSTED_REVIEWER_ID else "NONE",
    )


def _ci_observation(
    state: str,
    *,
    observed_at: datetime,
    observation_id: int,
) -> CIObservation:
    return CIObservation(
        name="quality",
        state=state,
        source="check",
        observed_at=observed_at,
        observation_id=observation_id,
    )
