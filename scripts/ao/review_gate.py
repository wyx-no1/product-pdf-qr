from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Protocol, cast

from scripts.ao.git import CommandFailed, CommandRunner

REQUIRED_CI_JOBS = frozenset({"quality", "database", "container"})
EVIDENCE_PREFIX = "docs/evidence/"
STALL_THRESHOLD = timedelta(minutes=30)
GITHUB_ACTIONS_APP_ID = 15368
GITHUB_ACTIONS_BOT_ID = 41898282
TERMINAL_CI_STATES = frozenset(
    {
        "action_required",
        "cancelled",
        "error",
        "failure",
        "neutral",
        "skipped",
        "stale",
        "startup_failure",
        "success",
        "timed_out",
    }
)
FILE_STATUSES = frozenset({"added", "changed", "copied", "modified", "removed", "renamed"})
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class VerdictGrammar:
    """One canonical verdict-heading language and its exact known values."""

    heading: re.Pattern[str]
    verdicts: Mapping[str, str]


VERDICT_GRAMMARS: tuple[VerdictGrammar, ...] = (
    VerdictGrammar(
        heading=re.compile(r"^## Review verdict: (.*)$", flags=re.MULTILINE),
        verdicts={"approved": "approved", "changes requested": "changes_requested"},
    ),
    VerdictGrammar(
        heading=re.compile(r"^## 审查结论：(.*)$", flags=re.MULTILINE),  # noqa: RUF001
        verdicts={"通过": "approved", "需要修改": "changes_requested"},
    ),
    VerdictGrammar(
        heading=re.compile(r"^## (Approved|Changes requested)$", flags=re.MULTILINE),
        verdicts={"Approved": "approved", "Changes requested": "changes_requested"},
    ),
)


class ReviewGateError(RuntimeError):
    """Review-gate input or GitHub evidence is unavailable or malformed."""


@dataclass(frozen=True)
class ReviewPullRequest:
    number: int
    repository: str
    title: str
    source_branch: str
    target_branch: str
    base_sha: str
    head_sha: str
    url: str


@dataclass(frozen=True)
class ReviewFile:
    filename: str
    status: str = "modified"
    previous_filename: str | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        if self.previous_filename is None:
            return (self.filename,)
        return (self.previous_filename, self.filename)


@dataclass(frozen=True)
class ReviewCommit:
    sha: str
    parent_sha: str
    tree_sha: str
    parent_tree_sha: str
    files: tuple[ReviewFile, ...]

    @property
    def classification(self) -> str:
        if self.tree_sha == self.parent_tree_sha:
            if self.files:
                raise ReviewGateError(
                    f"commit {self.sha} has an unchanged tree but reports changed files"
                )
            return "NOOP"
        if not self.files:
            raise ReviewGateError(
                f"commit {self.sha} changes its Git tree but returned no changed files"
            )
        return (
            "METADATA"
            if all(
                path.startswith(EVIDENCE_PREFIX)
                for changed_file in self.files
                for path in changed_file.paths
            )
            else "CODE"
        )

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(path for changed_file in self.files for path in changed_file.paths)

    @property
    def reason(self) -> str:
        if self.classification == "NOOP":
            return f"tree={self.tree_sha} equals parent_tree={self.parent_tree_sha}"
        rendered = ", ".join(
            (
                f"{changed_file.previous_filename} -> {changed_file.filename}"
                if changed_file.previous_filename is not None
                else changed_file.filename
            )
            for changed_file in self.files
        )
        if self.classification == "METADATA":
            return f"all new and previous paths are evidence paths: {rendered}"
        return f"contains a non-evidence new or previous path: {rendered}"


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
class CumulativeEvidence:
    base_sha: str
    final_sha: str
    attested_sha: str
    attested_parent_sha: str | None
    merge_base_sha: str
    changed_paths: tuple[str, ...]
    state: str
    source: str
    observed_at: datetime
    observation_id: int
    issuer_kind: str
    issuer_id: int
    complete: bool
    commit_count: int

    @property
    def trusted(self) -> bool:
        return (
            (
                self.source == "github-compare"
                and self.issuer_kind == "github-server"
                and self.issuer_id == 0
            )
            or (
                self.source == "evidence-check"
                and self.issuer_kind == "github-app"
                and self.issuer_id == GITHUB_ACTIONS_APP_ID
            )
            or (
                self.source == "evidence-status"
                and self.issuer_kind == "github-user"
                and self.issuer_id == GITHUB_ACTIONS_BOT_ID
            )
        )


@dataclass(frozen=True)
class CommitGateResult:
    sha: str
    classification: str
    ci_status: str
    verdict: str
    prior_verdict: str
    superseded_by: str | None
    reason: str
    gaps: tuple[str, ...]
    stalled: bool


@dataclass(frozen=True)
class ReviewGateResult:
    pr_number: int
    status: str
    final_code_sha: str | None
    commits: tuple[CommitGateResult, ...]

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "PASS" else 2

    def render(self) -> str:
        final = self.final_code_sha or "none"
        lines = [f"{self.status} PR #{self.pr_number} final_code_sha={final}"]
        for commit in self.commits:
            fields = [
                commit.classification,
                commit.sha,
                f"ci={commit.ci_status}",
                f"verdict={commit.verdict}",
            ]
            if commit.classification == "SUPERSEDED":
                fields.extend(
                    (
                        f"superseded_by={commit.superseded_by}",
                        f"prior_verdict={commit.prior_verdict}",
                    )
                )
            fields.append(f"reason={commit.reason}")
            lines.append(" ".join(fields))
            if commit.stalled:
                lines.append(
                    f"STALLED {commit.sha}: anchored review activity exceeded 30 minutes "
                    "without a delivered verdict"
                )
            lines.extend(f"REVIEW_GAP {commit.sha}: {gap}" for gap in commit.gaps)
        return "\n".join(lines)


class ReviewGateData(Protocol):
    def repository_owner_id(self) -> int: ...

    def pull_request(self, number: int) -> ReviewPullRequest: ...

    def pull_request_commits(self, number: int) -> tuple[ReviewCommit, ...]: ...

    def pull_request_reviews(self, number: int) -> tuple[ReviewRecord, ...]: ...

    def commit_ci(self, commit_sha: str) -> Mapping[str, CIObservation]: ...

    def cumulative_evidence(
        self,
        number: int,
        base_sha: str,
        final_sha: str,
    ) -> tuple[CumulativeEvidence, ...]: ...


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

    def _single(self, endpoint: str, label: str) -> dict[str, object]:
        pages = self._pages(endpoint)
        if len(pages) != 1 or not isinstance(pages[0], dict):
            raise ReviewGateError(f"GitHub returned an invalid {label}")
        return cast(dict[str, object], pages[0])

    def pull_request(self, number: int) -> ReviewPullRequest:
        data = self._single(f"repos/{self.repository}/pulls/{number}", f"PR #{number} record")
        head = _required_mapping(data, "head")
        base = _required_mapping(data, "base")
        return ReviewPullRequest(
            number=_required_int(data, "number"),
            repository=self.repository,
            title=_required_str(data, "title"),
            source_branch=_required_str(head, "ref"),
            target_branch=_required_str(base, "ref"),
            base_sha=_required_sha(base, "sha"),
            head_sha=_required_sha(head, "sha"),
            url=_required_str(data, "html_url"),
        )

    def repository_owner_id(self) -> int:
        data = self._single(f"repos/{self.repository}", f"repository {self.repository}")
        return _required_int(_required_mapping(data, "owner"), "id")

    def pull_request_commits(self, number: int) -> tuple[ReviewCommit, ...]:
        pages = self._pages(f"repos/{self.repository}/pulls/{number}/commits?per_page=100")
        listed_commits = _flatten_list_pages(pages, f"PR #{number} commits")
        if not listed_commits:
            raise ReviewGateError(f"PR #{number} has no commits")
        results: list[ReviewCommit] = []
        parent_trees: dict[str, str] = {}
        for listed in listed_commits:
            sha = _required_sha(listed, "sha")
            listed_parent = _single_parent(listed, sha)
            listed_tree = _required_sha(
                _required_mapping(_required_mapping(listed, "commit"), "tree"),
                "sha",
            )
            detail_pages = self._pages(f"repos/{self.repository}/commits/{sha}?per_page=100")
            files: list[ReviewFile] = []
            seen_files: set[str] = set()
            for detail_value in detail_pages:
                if not isinstance(detail_value, dict):
                    raise ReviewGateError(f"GitHub returned invalid commit details for {sha}")
                detail = cast(dict[str, object], detail_value)
                if _required_sha(detail, "sha") != sha:
                    raise ReviewGateError(f"GitHub returned mismatched commit details for {sha}")
                if _single_parent(detail, sha) != listed_parent:
                    raise ReviewGateError(f"GitHub returned conflicting parent data for {sha}")
                detail_tree = _required_sha(
                    _required_mapping(_required_mapping(detail, "commit"), "tree"),
                    "sha",
                )
                if detail_tree != listed_tree:
                    raise ReviewGateError(f"GitHub returned conflicting tree data for {sha}")
                files_value = detail.get("files")
                if not isinstance(files_value, list):
                    raise ReviewGateError(f"GitHub commit {sha} did not return changed files")
                for value in files_value:
                    changed_file = _parse_review_file(value, sha)
                    if changed_file.filename in seen_files:
                        raise ReviewGateError(
                            f"GitHub commit {sha} returned conflicting duplicate file "
                            f"{changed_file.filename}"
                        )
                    seen_files.add(changed_file.filename)
                    files.append(changed_file)
            parent_tree = parent_trees.get(listed_parent)
            if parent_tree is None:
                parent_data = self._single(
                    f"repos/{self.repository}/git/commits/{listed_parent}",
                    f"parent commit {listed_parent}",
                )
                if _required_sha(parent_data, "sha") != listed_parent:
                    raise ReviewGateError(f"GitHub returned mismatched parent {listed_parent}")
                parent_tree = _required_sha(_required_mapping(parent_data, "tree"), "sha")
                parent_trees[listed_parent] = parent_tree
            parent_trees[sha] = listed_tree
            results.append(
                ReviewCommit(
                    sha=sha,
                    parent_sha=listed_parent,
                    tree_sha=listed_tree,
                    parent_tree_sha=parent_tree,
                    files=tuple(files),
                )
            )
        return tuple(results)

    def pull_request_reviews(self, number: int) -> tuple[ReviewRecord, ...]:
        pages = self._pages(f"repos/{self.repository}/pulls/{number}/reviews?per_page=100")
        reviews = _flatten_list_pages(pages, f"PR #{number} reviews")
        records: list[ReviewRecord] = []
        for item in reviews:
            state = _required_str(item, "state")
            if state == "PENDING":
                continue
            author = _required_mapping(item, "user")
            records.append(
                ReviewRecord(
                    review_id=_required_int(item, "id"),
                    commit_id=_required_sha(item, "commit_id"),
                    state=state,
                    body=str(item.get("body") or ""),
                    submitted_at=_required_datetime(item, "submitted_at"),
                    url=_required_str(item, "html_url"),
                    author_id=_required_int(author, "id"),
                    author_login=_required_str(author, "login"),
                    author_type=_required_str(author, "type"),
                    author_association=_required_str(item, "author_association"),
                )
            )
        return tuple(records)

    def commit_ci(self, commit_sha: str) -> Mapping[str, CIObservation]:
        checks_pages = self._pages(
            f"repos/{self.repository}/commits/{commit_sha}/check-runs?filter=all&per_page=100"
        )
        statuses_pages = self._pages(
            f"repos/{self.repository}/commits/{commit_sha}/status?per_page=100"
        )
        checks = _check_observations(checks_pages, commit_sha)
        statuses = _status_observations(statuses_pages, commit_sha)
        latest_checks = _latest_observations(checks)
        latest_statuses = _latest_observations(statuses)
        result: dict[str, CIObservation] = {}
        for name in REQUIRED_CI_JOBS:
            if name in latest_checks:
                result[name] = latest_checks[name]
            elif name in latest_statuses:
                result[name] = latest_statuses[name]
        return result

    def cumulative_evidence(
        self,
        number: int,
        base_sha: str,
        final_sha: str,
    ) -> tuple[CumulativeEvidence, ...]:
        del number
        endpoint = f"repos/{self.repository}/compare/{base_sha}...{final_sha}?per_page=100"
        data = self._single(endpoint, f"compare evidence for {base_sha}...{final_sha}")
        if _required_sha(_required_mapping(data, "base_commit"), "sha") != base_sha:
            raise ReviewGateError("GitHub compare evidence returned the wrong base SHA")
        merge_base = _required_sha(_required_mapping(data, "merge_base_commit"), "sha")
        status = _required_str(data, "status")
        if status not in {"ahead", "diverged", "identical"}:
            raise ReviewGateError(f"GitHub compare returned unsupported status {status}")
        files_value = data.get("files")
        if not isinstance(files_value, list):
            raise ReviewGateError("GitHub compare evidence did not return changed files")
        if len(files_value) >= 300:
            raise ReviewGateError("GitHub compare changed-files result may be truncated at 300")
        changed_paths: list[str] = []
        seen: set[str] = set()
        for value in files_value:
            changed_file = _parse_review_file(value, final_sha)
            if changed_file.filename in seen:
                raise ReviewGateError(
                    f"GitHub compare returned duplicate file {changed_file.filename}"
                )
            seen.add(changed_file.filename)
            changed_paths.extend(changed_file.paths)
        return (
            CumulativeEvidence(
                base_sha=base_sha,
                final_sha=final_sha,
                attested_sha=final_sha,
                attested_parent_sha=None,
                merge_base_sha=merge_base,
                changed_paths=tuple(dict.fromkeys(changed_paths)),
                state="success",
                source="github-compare",
                observed_at=datetime(1970, 1, 1, tzinfo=UTC),
                observation_id=0,
                issuer_kind="github-server",
                issuer_id=0,
                complete=True,
                commit_count=_required_int(data, "ahead_by"),
            ),
        )


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
    initial_pr = github.pull_request(pr_number)
    commits = github.pull_request_commits(pr_number)
    _validate_current_chain(initial_pr, commits)
    classifications = tuple(commit.classification for commit in commits)
    code_indexes = tuple(index for index, value in enumerate(classifications) if value == "CODE")
    final_code_sha = commits[code_indexes[-1]].sha if code_indexes else None
    current_time = now()
    if current_time.tzinfo is None:
        raise ReviewGateError("review-gate clock must be timezone-aware")

    reviews: tuple[ReviewRecord, ...] = ()
    final_ci: Mapping[str, CIObservation] = {}
    evidence_gap: str | None = None
    if final_code_sha is not None:
        reviews = github.pull_request_reviews(pr_number)
        final_ci = github.commit_ci(final_code_sha)
        evidence = github.cumulative_evidence(pr_number, initial_pr.base_sha, final_code_sha)
        evidence_gap = _validate_cumulative_evidence(
            evidence,
            initial_pr,
            commits,
            classifications,
            final_code_sha,
        )

    next_code: dict[int, str] = {}
    pending_next: str | None = None
    for index in range(len(commits) - 1, -1, -1):
        if classifications[index] == "CODE":
            if pending_next is not None:
                next_code[index] = pending_next
            pending_next = commits[index].sha

    results: list[CommitGateResult] = []
    for index, commit in enumerate(commits):
        classification = classifications[index]
        if classification in {"METADATA", "NOOP"}:
            results.append(
                CommitGateResult(
                    sha=commit.sha,
                    classification=classification,
                    ci_status="exempt",
                    verdict="exempt",
                    prior_verdict="missing",
                    superseded_by=None,
                    reason=commit.reason,
                    gaps=(),
                    stalled=False,
                )
            )
            continue
        if commit.sha != final_code_sha:
            prior = _trusted_prior_verdict(reviews, commit.sha, trusted_ids)
            results.append(
                CommitGateResult(
                    sha=commit.sha,
                    classification="SUPERSEDED",
                    ci_status="exempt",
                    verdict="exempt",
                    prior_verdict=prior,
                    superseded_by=next_code[index],
                    reason=(
                        "replaced by the next CODE state; final cumulative approval accepts history"
                    ),
                    gaps=(),
                    stalled=False,
                )
            )
            continue

        gaps: list[str] = []
        missing_ci = sorted(REQUIRED_CI_JOBS - final_ci.keys())
        failed_ci = sorted(
            f"{name}={observation.state or 'unknown'}"
            for name, observation in final_ci.items()
            if observation.state != "success"
        )
        if missing_ci:
            gaps.append(f"missing required CI jobs: {', '.join(missing_ci)}")
        if failed_ci:
            gaps.append(f"required CI jobs not successful: {', '.join(failed_ci)}")
        verdict, review_gap, stalled = _final_verdict(
            reviews,
            commit.sha,
            trusted_ids,
            current_time,
        )
        if review_gap is not None:
            gaps.append(review_gap)
        if verdict != "approved":
            gaps.append(f"final code commit verdict must be approved, got {verdict}")
        if evidence_gap is not None:
            gaps.append(evidence_gap)
        results.append(
            CommitGateResult(
                sha=commit.sha,
                classification="CODE(final)",
                ci_status="success" if not missing_ci and not failed_ci else "gap",
                verdict=verdict,
                prior_verdict="missing",
                superseded_by=None,
                reason="last CODE commit in the validated current PR chain",
                gaps=tuple(gaps),
                stalled=stalled,
            )
        )

    final_pr = github.pull_request(pr_number)
    if final_pr.head_sha != initial_pr.head_sha or final_pr.base_sha != initial_pr.base_sha:
        raise ReviewGateError(
            f"PR #{pr_number} changed while review-gate was reading evidence "
            f"(start base/head={initial_pr.base_sha}/{initial_pr.head_sha}, "
            f"end={final_pr.base_sha}/{final_pr.head_sha})"
        )
    status = "REVIEW_GAP" if any(result.gaps for result in results) else "PASS"
    return ReviewGateResult(
        pr_number=pr_number,
        status=status,
        final_code_sha=final_code_sha,
        commits=tuple(results),
    )


def _validate_current_chain(
    pull_request: ReviewPullRequest,
    commits: Sequence[ReviewCommit],
) -> None:
    if not commits:
        raise ReviewGateError(f"PR #{pull_request.number} has no commits")
    seen: set[str] = set()
    for index, commit in enumerate(commits):
        for label, value in (
            ("commit SHA", commit.sha),
            ("parent SHA", commit.parent_sha),
            ("tree SHA", commit.tree_sha),
            ("parent tree SHA", commit.parent_tree_sha),
        ):
            _require_full_sha(value, label)
        if commit.sha in seen:
            raise ReviewGateError(f"PR #{pull_request.number} contains duplicate SHA {commit.sha}")
        seen.add(commit.sha)
        if index and commit.parent_sha != commits[index - 1].sha:
            raise ReviewGateError(
                f"PR #{pull_request.number} commit chain is not contiguous at {commit.sha}"
            )
        if index and commit.parent_tree_sha != commits[index - 1].tree_sha:
            raise ReviewGateError(
                f"PR #{pull_request.number} parent tree is inconsistent at {commit.sha}"
            )
    if commits[-1].sha != pull_request.head_sha:
        raise ReviewGateError(
            f"PR #{pull_request.number} commit list ends at {commits[-1].sha}, "
            f"but GitHub reports head {pull_request.head_sha}"
        )


def _validate_cumulative_evidence(
    observations: Sequence[CumulativeEvidence],
    pull_request: ReviewPullRequest,
    commits: Sequence[ReviewCommit],
    classifications: Sequence[str],
    final_sha: str,
) -> str | None:
    exact = tuple(
        observation
        for observation in observations
        if observation.trusted
        and observation.base_sha == pull_request.base_sha
        and observation.final_sha == final_sha
        and (
            observation.attested_sha == final_sha
            or (
                observation.attested_sha == pull_request.head_sha
                and observation.attested_parent_sha == final_sha
            )
        )
    )
    if not exact:
        return (
            f"final code commit {final_sha} lacks trusted cumulative "
            f"{pull_request.base_sha}...{final_sha} review input evidence"
        )
    compare_observations = tuple(
        observation for observation in exact if observation.source == "github-compare"
    )
    if not compare_observations:
        return (
            "trusted cumulative review input lacks authoritative GitHub Compare "
            "corroboration for the exact base/final"
        )
    authoritative = max(
        compare_observations,
        key=lambda item: (item.observed_at, item.observation_id),
    )
    authoritative_gap = _validate_cumulative_observation(
        authoritative,
        commits,
        final_sha,
        label="authoritative GitHub Compare",
    )
    if authoritative_gap is not None:
        return authoritative_gap
    expected_paths = _persistent_introduced_paths(commits, classifications, final_sha)
    missing_paths = sorted(expected_paths - set(authoritative.changed_paths))
    if missing_paths:
        return (
            "authoritative GitHub Compare omits CODE paths introduced and retained "
            "in the final tree: " + ", ".join(missing_paths)
        )

    selected = max(exact, key=lambda item: (item.observed_at, item.observation_id))
    selected_gap = _validate_cumulative_observation(
        selected,
        commits,
        final_sha,
        label="latest trusted cumulative review input",
    )
    if selected_gap is not None:
        return selected_gap
    if selected.source != "github-compare" and set(selected.changed_paths) != set(
        authoritative.changed_paths
    ):
        return (
            f"latest trusted cumulative review input from {selected.source} does not "
            "match the authoritative GitHub Compare net changed-path set"
        )
    return None


def _validate_cumulative_observation(
    observation: CumulativeEvidence,
    commits: Sequence[ReviewCommit],
    final_sha: str,
    *,
    label: str,
) -> str | None:
    if observation.state != "success":
        return f"{label} is {observation.state}, not success"
    if not observation.complete:
        return f"{label} is marked incomplete"
    if observation.merge_base_sha != commits[0].parent_sha:
        raise ReviewGateError(
            f"{label} merge base {observation.merge_base_sha} "
            f"does not match current chain parent {commits[0].parent_sha}"
        )
    final_index = next(index for index, commit in enumerate(commits) if commit.sha == final_sha)
    expected_commit_count = final_index + 1
    if observation.commit_count != expected_commit_count:
        raise ReviewGateError(
            f"{label} covers {observation.commit_count} commits, "
            f"but current chain through final contains {expected_commit_count}"
        )
    return None


def _persistent_introduced_paths(
    commits: Sequence[ReviewCommit],
    classifications: Sequence[str],
    final_sha: str,
) -> set[str]:
    """Conservatively track PR-created paths that must remain in the net diff."""

    introduced: set[str] = set()
    for commit, classification in zip(commits, classifications, strict=True):
        if classification == "CODE":
            for changed_file in commit.files:
                if changed_file.status in {"added", "copied"}:
                    introduced.add(changed_file.filename)
                elif changed_file.status == "removed":
                    introduced.discard(changed_file.filename)
                elif (
                    changed_file.status == "renamed"
                    and changed_file.previous_filename in introduced
                ):
                    introduced.remove(changed_file.previous_filename)
                    introduced.add(changed_file.filename)
        if commit.sha == final_sha:
            break
    return introduced


def _final_verdict(
    reviews: Sequence[ReviewRecord],
    sha: str,
    trusted_ids: frozenset[int],
    current_time: datetime,
) -> tuple[str, str | None, bool]:
    anchored = tuple(review for review in reviews if review.commit_id == sha)
    submitted = tuple(review for review in anchored if review.state not in {"DISMISSED", "PENDING"})
    trusted = tuple(review for review in submitted if review.author_id in trusted_ids)
    if not anchored:
        return "missing", "missing anchored review with commit_id exactly equal to final sha", False
    if not submitted:
        return "missing", "final anchored reviews are dismissed or pending", False
    if not trusted:
        authors = ", ".join(
            sorted({f"{review.author_login} (id={review.author_id})" for review in submitted})
        )
        return (
            "untrusted",
            f"final anchored reviews are not from a trusted reviewer: {authors}",
            False,
        )
    latest = max(trusted, key=lambda item: (item.submitted_at, item.review_id))
    verdict = _review_verdict(latest.body)
    if verdict is None:
        stalled = current_time - latest.submitted_at > STALL_THRESHOLD
        return (
            "indeterminate",
            "final review verdict cannot be determined from the latest trusted "
            "anchored GitHub review body",
            stalled,
        )
    return verdict, None, False


def _trusted_prior_verdict(
    reviews: Sequence[ReviewRecord],
    sha: str,
    trusted_ids: frozenset[int],
) -> str:
    trusted = tuple(
        review
        for review in reviews
        if review.commit_id == sha
        and review.author_id in trusted_ids
        and review.state not in {"DISMISSED", "PENDING"}
    )
    if not trusted:
        return "missing"
    latest = max(trusted, key=lambda item: (item.submitted_at, item.review_id))
    return _review_verdict(latest.body) or "missing"


def _review_verdict(body: str) -> str | None:
    normalized = _without_fenced_code(body.replace("\r\n", "\n"))
    matches = tuple(
        (grammar, match.group(1))
        for grammar in VERDICT_GRAMMARS
        for match in grammar.heading.finditer(normalized)
    )
    if len(matches) != 1:
        return None
    grammar, value = matches[0]
    return grammar.verdicts.get(value)


def _without_fenced_code(body: str) -> str:
    outside: list[str] = []
    fence: tuple[str, int] | None = None
    for line in body.splitlines():
        if fence is None:
            marker_match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if marker_match is None:
                outside.append(line)
                continue
            marker = marker_match.group(1)
            info = marker_match.group(2)
            if marker[0] == "`" and "`" in info:
                outside.append(line)
                continue
            fence = (marker[0], len(marker))
            outside.append("")
        else:
            marker, minimum_length = fence
            closing = re.fullmatch(
                rf" {{0,3}}{re.escape(marker)}{{{minimum_length},}}[ \t]*",
                line,
            )
            if closing is not None:
                fence = None
            outside.append("")
    return "\n".join(outside)


def _latest_observations(
    observations: Sequence[CIObservation],
) -> dict[str, CIObservation]:
    latest: dict[str, CIObservation] = {}
    for observation in observations:
        current = latest.get(observation.name)
        if current is None or (
            observation.state in TERMINAL_CI_STATES,
            observation.observed_at,
            observation.observation_id,
        ) > (
            current.state in TERMINAL_CI_STATES,
            current.observed_at,
            current.observation_id,
        ):
            latest[observation.name] = observation
    return latest


def _check_observations(
    pages: Sequence[object],
    commit_sha: str,
) -> tuple[CIObservation, ...]:
    observations: list[CIObservation] = []
    for value in pages:
        if not isinstance(value, dict):
            raise ReviewGateError(f"GitHub returned invalid check runs for {commit_sha}")
        page = cast(dict[str, object], value)
        runs = page.get("check_runs")
        if not isinstance(runs, list):
            raise ReviewGateError(f"GitHub commit {commit_sha} did not return check runs")
        for raw in runs:
            if not isinstance(raw, dict):
                raise ReviewGateError(f"GitHub commit {commit_sha} returned an invalid check run")
            item = cast(dict[str, object], raw)
            if _required_sha(item, "head_sha") != commit_sha:
                raise ReviewGateError(f"GitHub returned a check for the wrong SHA {commit_sha}")
            observations.append(
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
    return tuple(observations)


def _status_observations(
    pages: Sequence[object],
    commit_sha: str,
) -> tuple[CIObservation, ...]:
    observations: list[CIObservation] = []
    for value in pages:
        if not isinstance(value, dict):
            raise ReviewGateError(f"GitHub returned invalid statuses for {commit_sha}")
        page = cast(dict[str, object], value)
        if _required_sha(page, "sha") != commit_sha:
            raise ReviewGateError(f"GitHub returned statuses for the wrong SHA {commit_sha}")
        statuses = page.get("statuses")
        if not isinstance(statuses, list):
            raise ReviewGateError(f"GitHub commit {commit_sha} did not return statuses")
        for raw in statuses:
            if not isinstance(raw, dict):
                raise ReviewGateError(f"GitHub commit {commit_sha} returned an invalid status")
            item = cast(dict[str, object], raw)
            observations.append(
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
    return tuple(observations)


def _parse_review_file(value: object, commit_sha: str) -> ReviewFile:
    if not isinstance(value, dict):
        raise ReviewGateError(f"GitHub commit {commit_sha} returned an invalid file")
    item = cast(dict[str, object], value)
    filename = _required_path(item, "filename")
    status = _required_str(item, "status")
    if status not in FILE_STATUSES:
        raise ReviewGateError(f"GitHub commit {commit_sha} returned unknown file status {status}")
    previous_value = item.get("previous_filename")
    if status == "renamed":
        if previous_value is None:
            raise ReviewGateError(f"GitHub renamed file {filename} is missing previous_filename")
        previous = _required_path(item, "previous_filename")
        if previous == filename:
            raise ReviewGateError(f"GitHub renamed file {filename} has the same previous_filename")
    else:
        if previous_value is not None:
            raise ReviewGateError(
                f"GitHub non-renamed file {filename} unexpectedly has previous_filename"
            )
        previous = None
    return ReviewFile(filename=filename, status=status, previous_filename=previous)


def _required_path(data: dict[str, object], key: str) -> str:
    value = _required_str(data, key)
    parsed = PurePosixPath(value)
    if (
        value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or "\x00" in value
        or value in {".", ".."}
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or parsed.as_posix() != value
    ):
        raise ReviewGateError(f"GitHub field {key} is not a normalized repository path")
    return value


def _single_parent(data: dict[str, object], sha: str) -> str:
    parents = data.get("parents")
    if not isinstance(parents, list) or len(parents) != 1 or not isinstance(parents[0], dict):
        raise ReviewGateError(f"GitHub commit {sha} must have exactly one parent")
    return _required_sha(cast(dict[str, object], parents[0]), "sha")


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
    if not isinstance(value, int) or isinstance(value, bool):
        raise ReviewGateError(f"GitHub response is missing required integer field {key}")
    return value


def _required_sha(data: dict[str, object], key: str) -> str:
    value = _required_str(data, key)
    _require_full_sha(value, f"GitHub field {key}")
    return value


def _require_full_sha(value: str, label: str) -> None:
    if FULL_SHA.fullmatch(value) is None:
        raise ReviewGateError(f"{label} is not a full lowercase commit SHA")


def _required_datetime(data: dict[str, object], key: str) -> datetime:
    return _parse_datetime(_required_str(data, key), key)


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
