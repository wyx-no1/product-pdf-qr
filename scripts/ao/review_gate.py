from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from scripts.ao.git import CommandFailed, CommandRunner
from scripts.ao.models import PullRequest

REQUIRED_CI_JOBS = frozenset({"quality", "database", "container"})
EVIDENCE_PREFIX = "docs/evidence/"
STALL_THRESHOLD = timedelta(minutes=30)
VERDICT_HEADING_PATTERN = re.compile(
    r"^## Review verdict: (.*)$",
    flags=re.MULTILINE,
)
KNOWN_VERDICTS = {
    "approved": "approved",
    "changes requested": "changes_requested",
}


class ReviewGateError(RuntimeError):
    """Review-gate input or GitHub evidence is unavailable or malformed."""


@dataclass(frozen=True)
class ReviewCommit:
    sha: str
    paths: tuple[str, ...]

    @property
    def is_metadata(self) -> bool:
        return bool(self.paths) and all(path.startswith(EVIDENCE_PREFIX) for path in self.paths)


@dataclass(frozen=True)
class ReviewRecord:
    review_id: int
    commit_id: str
    state: str
    body: str
    submitted_at: datetime
    url: str
    author_id: int
    author_login: str
    author_type: str
    author_association: str


@dataclass(frozen=True)
class CIObservation:
    name: str
    state: str
    source: str
    observed_at: datetime
    observation_id: int


@dataclass(frozen=True)
class CommitGateResult:
    sha: str
    classification: str
    ci_status: str
    verdict: str
    gaps: tuple[str, ...]
    stalled: bool


@dataclass(frozen=True)
class ReviewGateResult:
    pr_number: int
    status: str
    commits: tuple[CommitGateResult, ...]

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "PASS" else 2

    def render(self) -> str:
        lines = [f"{self.status} PR #{self.pr_number}"]
        for commit in self.commits:
            lines.append(
                f"{commit.classification} {commit.sha} "
                f"ci={commit.ci_status} verdict={commit.verdict}"
            )
            if commit.stalled:
                lines.append(
                    f"STALLED {commit.sha}: anchored review activity exceeded 30 minutes "
                    "without a delivered verdict"
                )
            lines.extend(f"REVIEW_GAP {commit.sha}: {gap}" for gap in commit.gaps)
        return "\n".join(lines)


class ReviewGateData(Protocol):
    def repository_owner_id(self) -> int: ...

    def pull_request(self, number: int) -> PullRequest: ...

    def pull_request_commits(self, number: int) -> tuple[ReviewCommit, ...]: ...

    def pull_request_reviews(self, number: int) -> tuple[ReviewRecord, ...]: ...

    def commit_ci(self, commit_sha: str) -> Mapping[str, CIObservation]: ...


class GhReviewGateData:
    """Read immutable review-gate facts from GitHub through the gh CLI."""

    def __init__(self, repository: str, runner: CommandRunner | None = None) -> None:
        self.repository = repository
        self.runner = runner or CommandRunner()

    def _pages(self, endpoint: str) -> list[object]:
        try:
            result = self.runner.run(("gh", "api", "--paginate", "--slurp", endpoint))
        except FileNotFoundError as error:
            raise ReviewGateError("required command 'gh' is unavailable") from error
        except CommandFailed as error:
            raise ReviewGateError(f"GitHub API request failed for {endpoint}: {error}") from error
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ReviewGateError(f"gh returned invalid JSON for {endpoint}") from error
        if not isinstance(raw, list):
            raise ReviewGateError(f"GitHub API pagination returned a non-list for {endpoint}")
        return cast(list[object], raw)

    def pull_request(self, number: int) -> PullRequest:
        pages = self._pages(f"repos/{self.repository}/pulls/{number}")
        if len(pages) != 1 or not isinstance(pages[0], dict):
            raise ReviewGateError(f"GitHub returned an invalid pull request record for #{number}")
        data = cast(dict[str, object], pages[0])
        head = _required_mapping(data, "head")
        base = _required_mapping(data, "base")
        return PullRequest(
            number=_required_int(data, "number"),
            repository=self.repository,
            title=_required_str(data, "title"),
            source_branch=_required_str(head, "ref"),
            target_branch=_required_str(base, "ref"),
            head_sha=_required_sha(head, "sha"),
            url=_required_str(data, "html_url"),
            review_decision=str(data.get("review_decision") or "not-reviewed"),
        )

    def repository_owner_id(self) -> int:
        pages = self._pages(f"repos/{self.repository}")
        if len(pages) != 1 or not isinstance(pages[0], dict):
            raise ReviewGateError(
                f"GitHub returned an invalid repository record for {self.repository}"
            )
        owner = _required_mapping(cast(dict[str, object], pages[0]), "owner")
        return _required_int(owner, "id")

    def pull_request_commits(self, number: int) -> tuple[ReviewCommit, ...]:
        pages = self._pages(f"repos/{self.repository}/pulls/{number}/commits?per_page=100")
        commits = _flatten_list_pages(pages, f"PR #{number} commits")
        results: list[ReviewCommit] = []
        for item in commits:
            sha = _required_sha(item, "sha")
            detail_pages = self._pages(f"repos/{self.repository}/commits/{sha}?per_page=100")
            paths: list[str] = []
            for detail_value in detail_pages:
                if not isinstance(detail_value, dict):
                    raise ReviewGateError(f"GitHub returned invalid commit details for {sha}")
                detail = cast(dict[str, object], detail_value)
                if _required_sha(detail, "sha") != sha:
                    raise ReviewGateError(f"GitHub returned mismatched commit details for {sha}")
                files_value = detail.get("files")
                if not isinstance(files_value, list):
                    raise ReviewGateError(f"GitHub commit {sha} did not return changed files")
                for file_value in files_value:
                    if not isinstance(file_value, dict):
                        raise ReviewGateError(f"GitHub commit {sha} returned an invalid file")
                    paths.append(_required_str(cast(dict[str, object], file_value), "filename"))
            results.append(ReviewCommit(sha=sha, paths=tuple(dict.fromkeys(paths))))
        if not results:
            raise ReviewGateError(f"PR #{number} has no commits")
        return tuple(results)

    def pull_request_reviews(self, number: int) -> tuple[ReviewRecord, ...]:
        pages = self._pages(f"repos/{self.repository}/pulls/{number}/reviews?per_page=100")
        reviews = _flatten_list_pages(pages, f"PR #{number} reviews")
        return tuple(
            ReviewRecord(
                review_id=_required_int(item, "id"),
                commit_id=_required_sha(item, "commit_id"),
                state=_required_str(item, "state"),
                body=str(item.get("body") or ""),
                submitted_at=_required_datetime(item, "submitted_at"),
                url=_required_str(item, "html_url"),
                author_id=_required_int(_required_mapping(item, "user"), "id"),
                author_login=_required_str(_required_mapping(item, "user"), "login"),
                author_type=_required_str(_required_mapping(item, "user"), "type"),
                author_association=_required_str(item, "author_association"),
            )
            for item in reviews
            if item.get("state") != "PENDING"
        )

    def commit_ci(self, commit_sha: str) -> Mapping[str, CIObservation]:
        checks_pages = self._pages(
            f"repos/{self.repository}/commits/{commit_sha}/check-runs?filter=all&per_page=100"
        )
        statuses_pages = self._pages(
            f"repos/{self.repository}/commits/{commit_sha}/status?per_page=100"
        )
        checks: list[CIObservation] = []
        for page_value in checks_pages:
            if not isinstance(page_value, dict):
                raise ReviewGateError(f"GitHub returned invalid check runs for {commit_sha}")
            page = cast(dict[str, object], page_value)
            values = page.get("check_runs")
            if not isinstance(values, list):
                raise ReviewGateError(f"GitHub commit {commit_sha} did not return check runs")
            for value in values:
                if not isinstance(value, dict):
                    raise ReviewGateError(
                        f"GitHub commit {commit_sha} returned an invalid check run"
                    )
                item = cast(dict[str, object], value)
                checks.append(
                    CIObservation(
                        name=_required_str(item, "name"),
                        state=(
                            str(item.get("conclusion") or "")
                            if item.get("status") == "completed"
                            else str(item.get("status") or "")
                        ),
                        source="check",
                        observed_at=_optional_datetime(
                            item,
                            "completed_at",
                            fallback_key="started_at",
                        ),
                        observation_id=_required_int(item, "id"),
                    )
                )
        statuses: list[CIObservation] = []
        for page_value in statuses_pages:
            if not isinstance(page_value, dict):
                raise ReviewGateError(f"GitHub returned invalid statuses for {commit_sha}")
            page = cast(dict[str, object], page_value)
            values = page.get("statuses")
            if not isinstance(values, list):
                raise ReviewGateError(f"GitHub commit {commit_sha} did not return statuses")
            for value in values:
                if not isinstance(value, dict):
                    raise ReviewGateError(f"GitHub commit {commit_sha} returned an invalid status")
                item = cast(dict[str, object], value)
                statuses.append(
                    CIObservation(
                        name=_required_str(item, "context"),
                        state=_required_str(item, "state"),
                        source="status",
                        observed_at=_optional_datetime(
                            item,
                            "updated_at",
                            fallback_key="created_at",
                        ),
                        observation_id=_required_int(item, "id"),
                    )
                )
        latest_checks = _latest_observations(checks)
        latest_statuses = _latest_observations(statuses)
        result: dict[str, CIObservation] = {}
        for name in REQUIRED_CI_JOBS:
            if name in latest_checks:
                result[name] = latest_checks[name]
            elif name in latest_statuses:
                result[name] = latest_statuses[name]
        return result


def evaluate_review_gate(
    pr_number: int,
    github: ReviewGateData,
    *,
    trusted_reviewer_ids: frozenset[int] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ReviewGateResult:
    trusted_ids = (
        trusted_reviewer_ids
        if trusted_reviewer_ids is not None
        else frozenset({github.repository_owner_id()})
    )
    if not trusted_ids:
        raise ReviewGateError("at least one trusted reviewer ID is required")
    pull_request = github.pull_request(pr_number)
    commits = github.pull_request_commits(pr_number)
    if commits[-1].sha != pull_request.head_sha:
        raise ReviewGateError(
            f"PR #{pr_number} commit list ends at {commits[-1].sha}, "
            f"but GitHub reports head {pull_request.head_sha}"
        )
    reviews = github.pull_request_reviews(pr_number)
    code_commits = tuple(commit for commit in commits if not commit.is_metadata)
    final_code_sha = code_commits[-1].sha if code_commits else None
    current_time = now()
    if current_time.tzinfo is None:
        raise ReviewGateError("review-gate clock must be timezone-aware")

    results: list[CommitGateResult] = []
    for commit in commits:
        if commit.is_metadata:
            results.append(
                CommitGateResult(
                    sha=commit.sha,
                    classification="METADATA",
                    ci_status="exempt",
                    verdict="exempt",
                    gaps=(),
                    stalled=False,
                )
            )
            continue

        gaps: list[str] = []
        ci = github.commit_ci(commit.sha)
        missing_ci = sorted(REQUIRED_CI_JOBS - ci.keys())
        failed_ci = sorted(
            f"{name}={observation.state or 'unknown'}"
            for name, observation in ci.items()
            if observation.state != "success"
        )
        if missing_ci:
            gaps.append(f"missing required CI jobs: {', '.join(missing_ci)}")
        if failed_ci:
            gaps.append(f"required CI jobs not successful: {', '.join(failed_ci)}")
        ci_status = "success" if not missing_ci and not failed_ci else "gap"

        anchored = tuple(
            review
            for review in reviews
            if review.commit_id == commit.sha and review.state not in {"DISMISSED", "PENDING"}
        )
        trusted_anchored = tuple(review for review in anchored if review.author_id in trusted_ids)
        recognized = tuple(
            (review, verdict)
            for review in trusted_anchored
            if (verdict := _review_verdict(review.body)) is not None
        )
        if not anchored:
            verdict = "missing"
            gaps.append("missing anchored review with commit_id exactly equal to this sha")
        elif not trusted_anchored:
            verdict = "untrusted"
            authors = ", ".join(
                sorted({f"{review.author_login} (id={review.author_id})" for review in anchored})
            )
            gaps.append(f"anchored reviews are not from a trusted reviewer: {authors}")
        elif not recognized:
            verdict = "indeterminate"
            gaps.append(
                "review verdict cannot be determined from a trusted anchored GitHub review body"
            )
        else:
            latest_review, verdict = max(
                recognized,
                key=lambda item: (item[0].submitted_at, item[0].review_id),
            )
            del latest_review

        stalled = (
            not recognized
            and bool(trusted_anchored)
            and current_time - max(review.submitted_at for review in trusted_anchored)
            > STALL_THRESHOLD
        )
        if commit.sha == final_code_sha and verdict != "approved":
            gaps.append(f"final code commit verdict must be approved, got {verdict}")

        results.append(
            CommitGateResult(
                sha=commit.sha,
                classification="CODE",
                ci_status=ci_status,
                verdict=verdict,
                gaps=tuple(gaps),
                stalled=stalled,
            )
        )

    status = "REVIEW_GAP" if any(result.gaps for result in results) else "PASS"
    return ReviewGateResult(pr_number=pr_number, status=status, commits=tuple(results))


def _review_verdict(body: str) -> str | None:
    matches = tuple(
        match.group(1) for match in VERDICT_HEADING_PATTERN.finditer(body.replace("\r\n", "\n"))
    )
    if len(matches) != 1:
        return None
    return KNOWN_VERDICTS.get(matches[0])


def _latest_observations(
    observations: Sequence[CIObservation],
) -> dict[str, CIObservation]:
    latest: dict[str, CIObservation] = {}
    for observation in observations:
        current = latest.get(observation.name)
        if current is None or (
            observation.observed_at,
            observation.observation_id,
        ) > (
            current.observed_at,
            current.observation_id,
        ):
            latest[observation.name] = observation
    return latest


def _flatten_list_pages(pages: Sequence[object], label: str) -> list[dict[str, object]]:
    flattened: list[dict[str, object]] = []
    for page in pages:
        if not isinstance(page, list):
            raise ReviewGateError(f"GitHub returned an invalid page for {label}")
        for value in page:
            if not isinstance(value, dict):
                raise ReviewGateError(f"GitHub returned an invalid item for {label}")
            flattened.append(cast(dict[str, object], value))
    return flattened


def _required_mapping(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ReviewGateError(f"GitHub response is missing required object field {key}")
    return cast(dict[str, object], value)


def _required_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ReviewGateError(f"GitHub response is missing required field {key}")
    return value


def _required_int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise ReviewGateError(f"GitHub response is missing required integer field {key}")
    return value


def _required_sha(data: dict[str, object], key: str) -> str:
    value = _required_str(data, key)
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise ReviewGateError(f"GitHub field {key} is not a full lowercase commit SHA")
    return value


def _required_datetime(data: dict[str, object], key: str) -> datetime:
    value = _required_str(data, key)
    return _parse_datetime(value, key)


def _optional_datetime(
    data: dict[str, object],
    key: str,
    *,
    fallback_key: str,
) -> datetime:
    value = data.get(key) or data.get(fallback_key) or data.get("created_at")
    if not isinstance(value, str) or not value:
        raise ReviewGateError(
            f"GitHub response is missing timestamp fields {key}, {fallback_key}, and created_at"
        )
    return _parse_datetime(value, key)


def _parse_datetime(value: str, key: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReviewGateError(f"GitHub field {key} is not an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ReviewGateError(f"GitHub field {key} is not timezone-aware")
    return parsed
