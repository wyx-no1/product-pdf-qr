from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.ao.git import CommandFailed, CommandResult, CommandRunner
from scripts.ao.review_gate import (
    CIObservation,
    CumulativeEvidence,
    GhReviewGateData,
    ReviewCommit,
    ReviewFile,
    ReviewGateError,
    ReviewPullRequest,
    ReviewRecord,
    _latest_observations,
    _parse_review_file,
    _review_verdict,
    _single_parent,
    evaluate_review_gate,
)

NOW = datetime(2026, 8, 7, 8, 40, tzinfo=UTC)
TRUSTED_REVIEWER_ID = 306825498
BASE_SHA = "a" * 40
PR37_SHAS = (
    "617e3db323a0de4f771468478eb049842055fe80",
    "314bdfe42f7be83e4b6577d1159142dc7c783d5a",
    "ccf15096b677d91862d86737fd0c7c4281872a98",
    "a00026d446613bc9aaa63ea9f649c07a9755c607",
    "3ce07401765aae1e435d0f48b30087a9031aa5b2",
    "0141f56ba61441d7cd1b45453d04c2212630563b",
    "b6b366823e0fbcc2717faf6552a9961acd3ad64a",
    "fdcf814d9f29144bc3adaacd9c7503198b9533b7",
    "d10aa9d4c51f82d382e8c98fd19d29af9d6ae3d8",
    "617f0bb8fcd1d2a8b070ec75048404ba890c8718",
    "b8ccec9c9a577141366fb0f187c3f15b7f75740d",
    "fb62155898a97cf225046d810c8b06b4b941105f",
    "fd702448b52311db074ab56f3b1529ea12ba010d",
)


def _sha(value: int) -> str:
    return f"{value:040x}"


class FakeReviewGateData:
    def __init__(
        self,
        commits: tuple[ReviewCommit, ...],
        reviews: tuple[ReviewRecord, ...] | None = None,
        *,
        ci: Mapping[str, CIObservation] | None = None,
        evidence: tuple[CumulativeEvidence, ...] | None = None,
        moving_head: str | None = None,
        reported_head: str | None = None,
    ) -> None:
        self.commits = commits
        final = next(
            (commit.sha for commit in reversed(commits) if commit.classification == "CODE"),
            None,
        )
        self.reviews = (
            reviews
            if reviews is not None
            else ((_review(final, "approved"),) if final is not None else ())
        )
        self.ci = dict(ci) if ci is not None else _successful_ci()
        self.evidence = evidence if evidence is not None else _evidence_for(commits)
        self.moving_head = moving_head
        self.reported_head = reported_head
        self.pull_reads = 0
        self.ci_calls: list[str] = []
        self.review_calls = 0
        self.evidence_calls = 0

    def repository_owner_id(self) -> int:
        return TRUSTED_REVIEWER_ID

    def pull_request(self, number: int) -> ReviewPullRequest:
        self.pull_reads += 1
        head = self.reported_head or self.commits[-1].sha
        if self.moving_head is not None and self.pull_reads > 1:
            head = self.moving_head
        return ReviewPullRequest(
            number=number,
            repository="owner/repository",
            title="Review gate",
            source_branch="fix/review-gate",
            target_branch="main",
            base_sha=BASE_SHA,
            head_sha=head,
            url=f"https://example.invalid/pull/{number}",
        )

    def pull_request_commits(self, number: int) -> tuple[ReviewCommit, ...]:
        assert number in {18, 19, 37}
        return self.commits

    def pull_request_reviews(self, number: int) -> tuple[ReviewRecord, ...]:
        self.review_calls += 1
        return self.reviews

    def commit_ci(self, commit_sha: str) -> Mapping[str, CIObservation]:
        self.ci_calls.append(commit_sha)
        return self.ci

    def cumulative_evidence(
        self,
        number: int,
        base_sha: str,
        final_sha: str,
    ) -> tuple[CumulativeEvidence, ...]:
        del number, base_sha, final_sha
        self.evidence_calls += 1
        return self.evidence


def test_g03_38_01_g03_38_50_pr37_history_passes_and_is_replayable() -> None:
    commits = _chain(
        tuple(
            (
                sha,
                (ReviewFile(f"scripts/pr37/round-{index}.py"),),
                False,
            )
            for index, sha in enumerate(PR37_SHAS, start=1)
        )
    )
    github = FakeReviewGateData(
        commits,
        (_review(PR37_SHAS[-1], "approved", review_id=4881263827),),
    )

    result = evaluate_review_gate(37, github, now=lambda: NOW)

    assert result.status == "PASS"
    assert result.exit_code == 0
    assert result.final_code_sha == PR37_SHAS[-1]
    assert [item.classification for item in result.commits[:-1]] == ["SUPERSEDED"] * 12
    failed_history = result.commits[4]
    assert failed_history.sha == PR37_SHAS[4]
    assert failed_history.superseded_by == PR37_SHAS[5]
    assert failed_history.prior_verdict == "missing"
    assert result.commits[-1].classification == "CODE(final)"
    assert github.ci_calls == [PR37_SHAS[-1]]


def test_g03_38_02_pr37_synthetic_noop_and_metadata_tail_do_not_move_final() -> None:
    code = _chain(
        tuple(
            (sha, (ReviewFile(f"src/r{index}.py"),), False) for index, sha in enumerate(PR37_SHAS)
        )
    )
    tailed = _append(
        code,
        ("b" * 40, (), True),
        ("c" * 40, (ReviewFile("docs/evidence/pr-37/metadata.md"),), False),
    )
    github = FakeReviewGateData(tailed)

    result = evaluate_review_gate(37, github, now=lambda: NOW)

    assert result.status == "PASS"
    assert result.final_code_sha == PR37_SHAS[-1]
    assert [item.classification for item in result.commits[-2:]] == ["NOOP", "METADATA"]
    assert github.ci_calls == [PR37_SHAS[-1]]


@pytest.mark.parametrize("failure", ["ci", "review"])
def test_g03_38_03_pr37_final_condition_reversal_blocks(failure: str) -> None:
    commits = _chain(tuple((sha, (ReviewFile(f"src/{sha}.py"),), False) for sha in PR37_SHAS))
    ci = _successful_ci()
    reviews: tuple[ReviewRecord, ...] = (_review(PR37_SHAS[-1], "approved"),)
    if failure == "ci":
        ci["quality"] = replace(ci["quality"], state="failure")
    else:
        reviews = ()
    result = evaluate_review_gate(
        37,
        FakeReviewGateData(commits, reviews, ci=ci),
        now=lambda: NOW,
    )

    assert result.status == "REVIEW_GAP"
    assert result.exit_code == 2
    assert all(not item.gaps for item in result.commits[:-1])


def test_g03_38_04_g03_38_05_pr18_metadata_head_requires_exact_final_approval() -> None:
    shas = (
        "81611b27f1c49ab44dce2a2442f681f87950a949",
        "f3f9edaf49e8ee75cdd664137f18e909bddfb129",
        "2556120bad9a955147df33a90f1c447e35c96bfe",
        "53f06440357aedefba47f0366f7abf42f1c754ca",
    )
    commits = _chain(
        (
            (shas[0], (ReviewFile("src/auth.py"),), False),
            (shas[1], (ReviewFile("src/login.py"),), False),
            (shas[2], (ReviewFile("tests/test_login.py"),), False),
            (shas[3], (ReviewFile("docs/evidence/pr-18/metadata.md"),), False),
        )
    )
    reviews = (_review(shas[0], "changes requested"),)

    blocked = evaluate_review_gate(18, FakeReviewGateData(commits, reviews), now=lambda: NOW)
    passed = evaluate_review_gate(
        18,
        FakeReviewGateData(
            commits,
            (*reviews, _review(shas[2], "approved", review_id=2)),
        ),
        now=lambda: NOW,
    )

    assert blocked.status == "REVIEW_GAP"
    assert blocked.final_code_sha == shas[2]
    assert blocked.commits[-1].classification == "METADATA"
    assert passed.status == "PASS"
    assert [item.classification for item in passed.commits] == [
        "SUPERSEDED",
        "SUPERSEDED",
        "CODE(final)",
        "METADATA",
    ]


def test_g03_38_06_intermediate_file_is_in_cumulative_review_input() -> None:
    commits = _chain(
        (
            (_sha(1), (ReviewFile("scripts/security_guard.py", "added"),), False),
            (_sha(2), (ReviewFile("README.md"),), False),
        )
    )

    result = evaluate_review_gate(19, FakeReviewGateData(commits), now=lambda: NOW)

    assert result.status == "PASS"
    assert "scripts/security_guard.py" in _evidence_for(commits)[0].changed_paths
    assert result.commits[0].superseded_by == commits[1].sha


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("wrong_sha", "lacks trusted cumulative"),
        ("missing", "marked incomplete"),
        ("single_patch", "omits CODE paths"),
        ("omitted_file", "omits CODE paths"),
    ],
)
def test_g03_38_07_incomplete_or_wrong_cumulative_input_blocks(
    mutation: str,
    message: str,
) -> None:
    commits = _chain(
        (
            (_sha(1), (ReviewFile("scripts/security_guard.py", "added"),), False),
            (_sha(2), (ReviewFile("README.md"),), False),
        )
    )
    evidence = _evidence_for(commits)[0]
    if mutation == "wrong_sha":
        evidence = replace(evidence, final_sha=_sha(99))
    elif mutation == "missing":
        evidence = replace(evidence, complete=False)
    else:
        evidence = replace(evidence, changed_paths=("README.md",))

    result = evaluate_review_gate(
        19,
        FakeReviewGateData(commits, evidence=(evidence,)),
        now=lambda: NOW,
    )

    assert result.status == "REVIEW_GAP"
    assert message in result.render()


def test_g03_38_08_g03_38_36_author_sources_and_pr_summary_are_never_consumed() -> None:
    commits = _chain(((_sha(1), (ReviewFile("src/app.py"),), False),))
    forged = replace(
        _evidence_for(commits)[0],
        source="author-file",
        issuer_kind="github-user",
        issuer_id=TRUSTED_REVIEWER_ID,
    )
    github = FakeReviewGateData(commits, reviews=(), evidence=(forged,))

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "REVIEW_GAP"
    assert "lacks trusted cumulative" in result.render()
    assert "missing anchored review" in result.render()


@pytest.mark.parametrize(
    "old_path",
    [".github/workflows/ci.yml", "scripts/ao/review_gate.py", "src/product_pdf_qr/main.py"],
)
def test_g03_38_09_rename_code_to_evidence_stays_code(old_path: str) -> None:
    commit = _chain(
        (
            (
                _sha(1),
                (
                    ReviewFile(
                        "docs/evidence/attack/file",
                        "renamed",
                        previous_filename=old_path,
                    ),
                ),
                False,
            ),
        )
    )[0]

    assert commit.classification == "CODE"
    assert old_path in commit.reason


@pytest.mark.parametrize(
    "new_path",
    [".github/workflows/ci.yml", "scripts/tool.py", "src/x.py"],
)
def test_g03_38_10_rename_evidence_to_code_stays_code(new_path: str) -> None:
    commit = _chain(
        (
            (
                _sha(1),
                (ReviewFile(new_path, "renamed", "docs/evidence/x"),),
                False,
            ),
        )
    )[0]
    assert commit.classification == "CODE"


def test_g03_38_11_g03_38_12_only_all_evidence_rename_is_metadata() -> None:
    evidence = _chain(
        (
            (
                _sha(1),
                (
                    ReviewFile(
                        "docs/evidence/b/x",
                        "renamed",
                        "docs/evidence/a/x",
                    ),
                ),
                False,
            ),
        )
    )[0]
    mixed = replace(
        evidence,
        files=(
            *evidence.files,
            ReviewFile("docs/evidence/c/y", "renamed", "scripts/security.py"),
        ),
    )

    assert evidence.classification == "METADATA"
    assert mixed.classification == "CODE"


@pytest.mark.parametrize(
    "raw",
    [
        {"filename": "docs/evidence/x", "status": "renamed"},
        {"filename": "docs/evidence/x", "status": "renamed", "previous_filename": None},
        {"filename": "docs/evidence/x", "status": "renamed", "previous_filename": ""},
        {"filename": "docs/evidence/x", "status": "renamed", "previous_filename": 1},
        {"filename": "docs/evidence/x", "status": "renamed", "previous_filename": "../x"},
        {"status": "modified"},
        "not-an-object",
    ],
)
def test_g03_38_13_malformed_previous_filename_fails_closed(raw: object) -> None:
    with pytest.raises(ReviewGateError):
        _parse_review_file(raw, _sha(1))


@pytest.mark.parametrize(
    "raw",
    [
        {"filename": "x", "status": "renamed", "previous_filename": "x"},
        {"filename": "x", "status": "modified", "previous_filename": "y"},
        {"filename": "x", "status": "unknown"},
    ],
)
def test_g03_38_14_rename_status_conflicts_fail_closed(raw: object) -> None:
    with pytest.raises(ReviewGateError):
        _parse_review_file(raw, _sha(1))


def test_g03_38_15_true_noop_uses_equal_trees_and_skips_gate_calls() -> None:
    commits = _chain(((_sha(1), (), True),))
    github = FakeReviewGateData(commits)

    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "PASS"
    assert result.final_code_sha is None
    assert result.commits[0].classification == "NOOP"
    assert "equals parent_tree" in result.commits[0].reason
    assert github.ci_calls == []
    assert github.review_calls == 0


def test_g03_38_16_empty_files_with_changed_tree_fails_closed() -> None:
    commits = _chain(((_sha(1), (), False),))
    github = FakeReviewGateData.__new__(FakeReviewGateData)
    github.commits = commits
    with pytest.raises(ReviewGateError, match="changes its Git tree"):
        _ = commits[0].classification


@pytest.mark.parametrize(
    "mutation",
    [
        "bad_parent",
        "bad_tree",
        "bad_parent_tree",
        "duplicate",
        "not_contiguous",
        "bad_parent_shape",
    ],
)
def test_g03_38_17_and_39_chain_or_tree_evidence_errors_fail_closed(mutation: str) -> None:
    commits = list(
        _chain(
            (
                (_sha(1), (ReviewFile("src/a.py"),), False),
                (_sha(2), (ReviewFile("src/b.py"),), False),
            )
        )
    )
    if mutation == "bad_parent":
        commits[0] = replace(commits[0], parent_sha="short")
    elif mutation == "bad_tree":
        commits[0] = replace(commits[0], tree_sha="short")
    elif mutation == "bad_parent_tree":
        commits[1] = replace(commits[1], parent_tree_sha=_sha(999))
    elif mutation == "duplicate":
        commits[1] = replace(commits[1], sha=commits[0].sha)
    elif mutation == "not_contiguous":
        commits[1] = replace(commits[1], parent_sha=_sha(999))
    else:
        with pytest.raises(ReviewGateError, match="exactly one parent"):
            _single_parent({"parents": []}, _sha(1))
        return

    with pytest.raises(ReviewGateError):
        evaluate_review_gate(19, FakeReviewGateData(tuple(commits)), now=lambda: NOW)


def test_g03_38_18_equal_tree_with_files_fails_closed() -> None:
    commit = ReviewCommit(
        sha=_sha(1),
        parent_sha=BASE_SHA,
        tree_sha=_sha(101),
        parent_tree_sha=_sha(101),
        files=(ReviewFile("src/x.py"),),
    )
    with pytest.raises(ReviewGateError, match="unchanged tree"):
        _ = commit.classification


def test_g03_38_19_g03_38_20_noop_and_metadata_tail_keep_code_final() -> None:
    commits = _chain(
        (
            (_sha(1), (ReviewFile("src/a.py"),), False),
            (_sha(2), (), True),
            (_sha(3), (ReviewFile("docs/evidence/pr-19/metadata.md"),), False),
        )
    )
    github = FakeReviewGateData(commits)
    result = evaluate_review_gate(19, github, now=lambda: NOW)

    assert result.status == "PASS"
    assert result.final_code_sha == commits[0].sha
    assert [item.classification for item in result.commits] == [
        "CODE(final)",
        "NOOP",
        "METADATA",
    ]
    assert github.ci_calls == [commits[0].sha]


def test_g03_38_21_g03_38_41_mixed_chain_supersedes_only_to_next_code() -> None:
    commits = _chain(
        (
            (_sha(1), (ReviewFile("src/a.py"),), False),
            (_sha(2), (ReviewFile("docs/evidence/a"),), False),
            (_sha(3), (), True),
            (_sha(4), (ReviewFile("src/b.py"),), False),
            (_sha(5), (), True),
            (_sha(6), (ReviewFile("docs/evidence/b"),), False),
            (_sha(7), (ReviewFile("src/c.py"),), False),
            (_sha(8), (ReviewFile("docs/evidence/c"),), False),
        )
    )
    result = evaluate_review_gate(19, FakeReviewGateData(commits), now=lambda: NOW)

    assert result.status == "PASS"
    assert result.commits[0].superseded_by == commits[3].sha
    assert result.commits[3].superseded_by == commits[6].sha
    assert all(
        item.superseded_by is None
        for item in result.commits
        if item.classification in {"METADATA", "NOOP", "CODE(final)"}
    )


def test_g03_38_21_net_compare_allows_transient_and_rename_paths() -> None:
    add_then_delete = _chain(
        (
            (_sha(1), (ReviewFile("src/transient.py", "added"),), False),
            (_sha(2), (ReviewFile("src/transient.py", "removed"),), False),
        )
    )
    transient_evidence = replace(_evidence_for(add_then_delete)[0], changed_paths=())
    transient_result = evaluate_review_gate(
        19,
        FakeReviewGateData(add_then_delete, evidence=(transient_evidence,)),
        now=lambda: NOW,
    )

    rename_twice = _chain(
        (
            (
                _sha(3),
                (ReviewFile("src/intermediate.py", "renamed", "src/original.py"),),
                False,
            ),
            (
                _sha(4),
                (ReviewFile("src/final.py", "renamed", "src/intermediate.py"),),
                False,
            ),
        )
    )
    rename_evidence = replace(
        _evidence_for(rename_twice)[0],
        changed_paths=("src/original.py", "src/final.py"),
    )
    rename_result = evaluate_review_gate(
        19,
        FakeReviewGateData(rename_twice, evidence=(rename_evidence,)),
        now=lambda: NOW,
    )

    assert transient_result.status == "PASS"
    assert rename_result.status == "PASS"
    assert "src/intermediate.py" not in rename_evidence.changed_paths


@pytest.mark.parametrize(
    "specs",
    [
        ((_sha(1), (), True), (_sha(2), (), True)),
        (
            (_sha(1), (ReviewFile("docs/evidence/a"),), False),
            (_sha(2), (ReviewFile("docs/evidence/b"),), False),
        ),
        (
            (_sha(1), (ReviewFile("docs/evidence/a"),), False),
            (_sha(2), (), True),
            (_sha(3), (ReviewFile("docs/evidence/b"),), False),
        ),
    ],
)
def test_g03_38_22_g03_38_23_g03_38_24_no_code_prs_pass_without_gate_calls(
    specs: tuple[tuple[str, tuple[ReviewFile, ...], bool], ...],
) -> None:
    github = FakeReviewGateData(_chain(specs))
    result = evaluate_review_gate(19, github, now=lambda: NOW)
    assert result.status == "PASS"
    assert result.final_code_sha is None
    assert all(item.classification in {"NOOP", "METADATA"} for item in result.commits)
    assert github.ci_calls == []
    assert github.review_calls == 0
    assert github.evidence_calls == 0


@pytest.mark.parametrize("missing", ["quality", "database", "container"])
def test_g03_38_25_each_missing_ci_job_blocks(missing: str) -> None:
    ci = _successful_ci()
    del ci[missing]
    result = evaluate_review_gate(
        19,
        FakeReviewGateData(_one_code(), ci=ci),
        now=lambda: NOW,
    )
    assert result.status == "REVIEW_GAP"
    assert f"missing required CI jobs: {missing}" in result.render()


@pytest.mark.parametrize("job", ["quality", "database", "container"])
@pytest.mark.parametrize(
    "state",
    [
        "queued",
        "pending",
        "in_progress",
        "cancelled",
        "skipped",
        "failure",
        "error",
        "timed_out",
        "neutral",
        "stale",
        "action_required",
        "startup_failure",
        "",
        "unknown",
    ],
)
def test_g03_38_26_every_non_success_ci_state_blocks(job: str, state: str) -> None:
    ci = _successful_ci()
    ci[job] = replace(ci[job], state=state)
    result = evaluate_review_gate(
        19,
        FakeReviewGateData(_one_code(), ci=ci),
        now=lambda: NOW,
    )
    assert result.status == "REVIEW_GAP"
    assert f"{job}={state or 'unknown'}" in result.render()


def test_g03_38_27_ci_observation_selection_is_deterministic() -> None:
    observations = (
        _ci_observation("success", 1, NOW),
        _ci_observation("in_progress", 2, NOW + timedelta(minutes=1)),
        _ci_observation("failure", 3, NOW + timedelta(minutes=2)),
    )
    assert _latest_observations(observations)["quality"].state == "failure"
    tied = (
        _ci_observation("success", 1, NOW),
        _ci_observation("failure", 2, NOW),
    )
    assert _latest_observations(tied)["quality"].state == "failure"


@pytest.mark.parametrize(
    ("author_id", "login", "association"),
    [(999, "attacker", "NONE"), (999, "wyx-no1", "OWNER"), (999, "attacker", "OWNER")],
)
def test_g03_38_28_reviewer_trust_uses_numeric_id(
    author_id: int,
    login: str,
    association: str,
) -> None:
    review = replace(
        _review(_one_code()[0].sha, "approved"),
        author_id=author_id,
        author_login=login,
        author_association=association,
    )
    result = evaluate_review_gate(
        19,
        FakeReviewGateData(_one_code(), (review,)),
        now=lambda: NOW,
    )
    assert result.status == "REVIEW_GAP"
    assert result.commits[0].verdict == "untrusted"


@pytest.mark.parametrize("state", ["DISMISSED", "PENDING"])
def test_g03_38_29_dismissed_or_pending_approval_is_unusable(state: str) -> None:
    sha = _one_code()[0].sha
    result = evaluate_review_gate(
        19,
        FakeReviewGateData(_one_code(), (replace(_review(sha, "approved"), state=state),)),
        now=lambda: NOW,
    )
    assert result.status == "REVIEW_GAP"
    assert result.commits[0].verdict == "missing"


def test_g03_38_29_production_parser_ignores_pending_review_fields() -> None:
    class StaticReviewPages(GhReviewGateData):
        def _pages(self, endpoint: str) -> list[object]:
            assert endpoint.endswith("/pulls/19/reviews?per_page=100")
            return [
                [
                    {
                        "id": 1,
                        "state": "PENDING",
                        "commit_id": None,
                        "submitted_at": None,
                        "user": None,
                    },
                    {
                        "id": 2,
                        "state": "APPROVED",
                        "commit_id": _sha(1),
                        "body": "## Approved",
                        "submitted_at": NOW.isoformat(),
                        "html_url": "https://example.invalid/reviews/2",
                        "user": {
                            "id": TRUSTED_REVIEWER_ID,
                            "login": "trusted-reviewer",
                            "type": "User",
                        },
                        "author_association": "OWNER",
                    },
                ]
            ]

    reviews = StaticReviewPages("owner/repository").pull_request_reviews(19)

    assert len(reviews) == 1
    assert reviews[0].review_id == 2
    assert reviews[0].state == "APPROVED"


def test_g03_38_30_g03_38_31_latest_trusted_review_wins() -> None:
    sha = _one_code()[0].sha
    earlier = _review(sha, "approved", review_id=1)
    later_changes = replace(
        _review(sha, "changes requested", review_id=2),
        submitted_at=NOW + timedelta(seconds=1),
    )
    blocked = evaluate_review_gate(
        19,
        FakeReviewGateData(_one_code(), (earlier, later_changes)),
        now=lambda: NOW + timedelta(seconds=2),
    )
    later_approval = replace(
        _review(sha, "approved", review_id=3),
        submitted_at=NOW + timedelta(seconds=2),
    )
    passed = evaluate_review_gate(
        19,
        FakeReviewGateData(_one_code(), (later_changes, later_approval)),
        now=lambda: NOW + timedelta(seconds=3),
    )
    assert blocked.commits[0].verdict == "changes_requested"
    assert blocked.status == "REVIEW_GAP"
    assert passed.status == "PASS"


def test_g03_38_32_g03_38_33_wrong_sha_approvals_do_not_cover_final() -> None:
    commits = _chain(
        (
            (_sha(1), (ReviewFile("src/a.py"),), False),
            (_sha(2), (ReviewFile("src/b.py"),), False),
            (_sha(3), (ReviewFile("docs/evidence/x"),), False),
            (_sha(4), (), True),
        )
    )
    reviews = (
        _review(commits[0].sha, "approved"),
        _review(commits[2].sha, "approved", review_id=2),
        _review(commits[3].sha, "approved", review_id=3),
    )
    result = evaluate_review_gate(19, FakeReviewGateData(commits, reviews), now=lambda: NOW)
    assert result.status == "REVIEW_GAP"
    assert result.final_code_sha == commits[1].sha
    assert result.commits[1].verdict == "missing"


@pytest.mark.parametrize(
    "body",
    [
        "## Review verdict: approved",
        "## 审查结论：通过",  # noqa: RUF001
        "## Approved",
    ],
)
def test_g03_38_34_verdict_valid_grammar(body: str) -> None:
    assert _review_verdict(body) == "approved"


@pytest.mark.parametrize(
    "body",
    [
        "No title",
        "## Review verdict: unknown",
        "## Review verdict: Approved",
        "## Review verdict: approved ",
        "### Review verdict: approved",
        "```\n## Approved\n```",
        " ````\n## Approved\n ```",
        "   ~~~~\n## Approved\n   ~~~",
        " ```\n## Approved\n ~~~\n ```",
        "## 审查结论：通过（Ready）",  # noqa: RUF001
        "## Review verdict: 通过",
        "## Approved\n## Approved",
        "## Approved\n## 审查结论：通过",  # noqa: RUF001
        "## Approved\n## Changes requested",
        "## Approved\n## Review verdict: unknown",
    ],
)
def test_g03_38_35_verdict_variants_fail_closed(body: str) -> None:
    assert _review_verdict(body) is None


@pytest.mark.parametrize(
    "body",
    [
        " ````\n## Changes requested\n ````\n## Approved",
        "   ~~~ info\n## Changes requested\n   ~~~~~\n## Approved",
    ],
)
def test_g03_38_35_valid_indented_fences_close_by_commonmark_rules(body: str) -> None:
    assert _review_verdict(body) == "approved"


def test_g03_38_37_head_and_commit_list_mismatch_errors() -> None:
    commits = _one_code()
    github = FakeReviewGateData(commits, reported_head=_sha(99))
    with pytest.raises(ReviewGateError, match="commit list ends"):
        evaluate_review_gate(19, github, now=lambda: NOW)


def test_g03_38_38_head_advance_during_read_fails_without_partial_pass() -> None:
    with pytest.raises(ReviewGateError, match="changed while"):
        evaluate_review_gate(
            19,
            FakeReviewGateData(_one_code(), moving_head=_sha(99)),
            now=lambda: NOW,
        )


@pytest.mark.parametrize("mutation", ["truncated", "wrong_merge_base"])
def test_g03_38_39_compare_proves_current_chain_is_complete(mutation: str) -> None:
    commits = _chain(
        (
            (_sha(1), (ReviewFile("src/a.py"),), False),
            (_sha(2), (ReviewFile("src/b.py"),), False),
        )
    )
    evidence = _evidence_for(commits)[0]
    if mutation == "truncated":
        evidence = replace(evidence, commit_count=3)
    else:
        evidence = replace(evidence, merge_base_sha=_sha(99))
    with pytest.raises(ReviewGateError):
        evaluate_review_gate(
            19,
            FakeReviewGateData(commits, evidence=(evidence,)),
            now=lambda: NOW,
        )


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


class FailingStageData(FakeReviewGateData):
    def __init__(self, stage: str) -> None:
        super().__init__(_one_code())
        self.stage = stage

    def pull_request_commits(self, number: int) -> tuple[ReviewCommit, ...]:
        if self.stage == "commits":
            raise ReviewGateError("commits stage failed")
        return super().pull_request_commits(number)

    def pull_request_reviews(self, number: int) -> tuple[ReviewRecord, ...]:
        if self.stage == "reviews":
            raise ReviewGateError("reviews stage failed")
        return super().pull_request_reviews(number)

    def commit_ci(self, commit_sha: str) -> Mapping[str, CIObservation]:
        if self.stage == "ci":
            raise ReviewGateError("CI stage failed")
        return super().commit_ci(commit_sha)

    def cumulative_evidence(
        self,
        number: int,
        base_sha: str,
        final_sha: str,
    ) -> tuple[CumulativeEvidence, ...]:
        if self.stage == "evidence":
            raise ReviewGateError("evidence stage failed")
        return super().cumulative_evidence(number, base_sha, final_sha)


@pytest.mark.parametrize("stage", ["commits", "reviews", "ci", "evidence"])
def test_g03_38_40_each_evidence_stage_error_propagates(stage: str) -> None:
    with pytest.raises(ReviewGateError, match=f"{stage.upper() if stage == 'ci' else stage} stage"):
        evaluate_review_gate(19, FailingStageData(stage), now=lambda: NOW)


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(),
        CommandFailed(("gh", "api", "endpoint"), 1, "network unavailable"),
    ],
)
def test_g03_38_40_api_failures_never_degrade_to_pass(error: Exception) -> None:
    github = GhReviewGateData("owner/repository", runner=FailingRunner(error))
    with pytest.raises(ReviewGateError):
        github.pull_request(19)


def test_g03_38_42_superseded_ci_and_review_never_block() -> None:
    commits = _chain(
        (
            (_sha(1), (ReviewFile("src/a.py"),), False),
            (_sha(2), (ReviewFile("src/b.py"),), False),
            (_sha(3), (ReviewFile("src/c.py"),), False),
        )
    )
    reviews = (
        _review(commits[0].sha, "changes requested"),
        _review(commits[1].sha, "approved", review_id=2),
        _review(commits[2].sha, "approved", review_id=3),
    )
    github = FakeReviewGateData(commits, reviews)
    result = evaluate_review_gate(19, github, now=lambda: NOW)
    assert result.status == "PASS"
    assert github.ci_calls == [commits[-1].sha]
    assert [item.prior_verdict for item in result.commits[:2]] == [
        "changes_requested",
        "approved",
    ]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("trusted", "approved"),
        ("untrusted", "missing"),
        ("dismissed", "missing"),
        ("wrong_sha", "missing"),
        ("ambiguous", "missing"),
    ],
)
def test_g03_38_43_prior_verdict_uses_only_trusted_exact_review(
    mutation: str,
    expected: str,
) -> None:
    commits = _chain(
        (
            (_sha(1), (ReviewFile("src/a.py"),), False),
            (_sha(2), (ReviewFile("src/b.py"),), False),
        )
    )
    prior = _review(commits[0].sha, "approved")
    if mutation == "untrusted":
        prior = replace(prior, author_id=999)
    elif mutation == "dismissed":
        prior = replace(prior, state="DISMISSED")
    elif mutation == "wrong_sha":
        prior = replace(prior, commit_id=_sha(99))
    elif mutation == "ambiguous":
        prior = replace(prior, body="## Approved\n## Changes requested")
    result = evaluate_review_gate(
        19,
        FakeReviewGateData(commits, (prior, _review(commits[1].sha, "approved", review_id=2))),
        now=lambda: NOW,
    )
    assert result.commits[0].prior_verdict == expected


@pytest.mark.parametrize(
    ("mutation", "passes"),
    [
        ("trusted", True),
        ("untrusted_publisher", False),
        ("wrong_sha", False),
        ("newer_failure", False),
        ("author_forge", False),
        ("metadata_unbound", False),
    ],
)
def test_g03_38_44_evidence_issuer_sha_and_latest_state_are_bound(
    mutation: str,
    passes: bool,
) -> None:
    commits = _one_code()
    authoritative = _evidence_for(commits)[0]
    trusted = replace(
        authoritative,
        source="evidence-check",
        issuer_kind="github-app",
        issuer_id=15368,
        observed_at=NOW + timedelta(seconds=1),
        observation_id=2,
    )
    evidence: tuple[CumulativeEvidence, ...] = (authoritative, trusted)
    if mutation == "untrusted_publisher":
        evidence = (replace(trusted, issuer_id=999),)
    elif mutation == "wrong_sha":
        evidence = (replace(trusted, final_sha=_sha(99)),)
    elif mutation == "newer_failure":
        evidence = (
            authoritative,
            trusted,
            replace(
                trusted,
                state="failure",
                observed_at=NOW + timedelta(seconds=2),
                observation_id=3,
            ),
        )
    elif mutation == "author_forge":
        evidence = (
            replace(
                trusted,
                source="author-file",
                issuer_kind="github-user",
                issuer_id=TRUSTED_REVIEWER_ID,
            ),
        )
    elif mutation == "metadata_unbound":
        evidence = (
            replace(
                trusted,
                source="evidence-status",
                issuer_kind="github-user",
                issuer_id=41898282,
                attested_sha=_sha(97),
                attested_parent_sha=_sha(99),
            ),
        )
    result = evaluate_review_gate(
        19,
        FakeReviewGateData(commits, evidence=evidence),
        now=lambda: NOW,
    )
    assert (result.status == "PASS") is passes


def test_g03_38_44_metadata_attestation_must_bind_parent_code_to_final() -> None:
    commits = _chain(
        (
            (_sha(1), (ReviewFile("src/app.py"),), False),
            (_sha(2), (ReviewFile("docs/evidence/pr-19/metadata.md"),), False),
        )
    )
    authoritative = _evidence_for(commits)[0]
    evidence = replace(
        authoritative,
        source="evidence-status",
        issuer_kind="github-user",
        issuer_id=41898282,
        attested_sha=commits[-1].sha,
        attested_parent_sha=commits[0].sha,
        observed_at=NOW + timedelta(seconds=1),
        observation_id=2,
    )
    passed = evaluate_review_gate(
        19,
        FakeReviewGateData(commits, evidence=(authoritative, evidence)),
        now=lambda: NOW,
    )
    unbound = replace(evidence, attested_parent_sha=_sha(99))
    blocked = evaluate_review_gate(
        19,
        FakeReviewGateData(commits, evidence=(unbound,)),
        now=lambda: NOW,
    )
    assert passed.status == "PASS"
    assert blocked.status == "REVIEW_GAP"


@pytest.mark.parametrize(
    ("source", "issuer_kind", "issuer_id"),
    [
        ("evidence-check", "github-app", 15368),
        ("evidence-status", "github-user", 41898282),
    ],
)
@pytest.mark.parametrize(
    "changed_file",
    [
        ReviewFile("src/app.py", "modified"),
        ReviewFile("src/removed.py", "removed"),
        ReviewFile("src/new.py", "renamed", "src/old.py"),
    ],
)
def test_g03_38_44_alternate_evidence_must_match_authoritative_net_diff(
    source: str,
    issuer_kind: str,
    issuer_id: int,
    changed_file: ReviewFile,
) -> None:
    commits = _chain(((_sha(1), (changed_file,), False),))
    authoritative = _evidence_for(commits)[0]
    incomplete_alternate = replace(
        authoritative,
        changed_paths=(),
        source=source,
        issuer_kind=issuer_kind,
        issuer_id=issuer_id,
        observed_at=NOW + timedelta(seconds=1),
        observation_id=2,
    )

    result = evaluate_review_gate(
        19,
        FakeReviewGateData(
            commits,
            evidence=(authoritative, incomplete_alternate),
        ),
        now=lambda: NOW,
    )

    assert result.status == "REVIEW_GAP"
    assert "does not match the authoritative GitHub Compare" in result.render()


def test_g03_38_44_alternate_evidence_requires_authoritative_compare() -> None:
    commits = _one_code()
    alternate = replace(
        _evidence_for(commits)[0],
        source="evidence-check",
        issuer_kind="github-app",
        issuer_id=15368,
    )

    result = evaluate_review_gate(
        19,
        FakeReviewGateData(commits, evidence=(alternate,)),
        now=lambda: NOW,
    )

    assert result.status == "REVIEW_GAP"
    assert "lacks authoritative GitHub Compare corroboration" in result.render()


def test_g03_38_45_force_pushed_old_shas_do_not_enter_current_result() -> None:
    current = _chain(
        (
            (_sha(10), (ReviewFile("src/new.py"),), False),
            (_sha(11), (ReviewFile("src/final.py"),), False),
        )
    )
    old_review = _review(_sha(1), "approved")
    final_review = _review(current[-1].sha, "approved", review_id=2)
    result = evaluate_review_gate(
        19,
        FakeReviewGateData(current, (old_review, final_review)),
        now=lambda: NOW,
    )
    assert [item.sha for item in result.commits] == [item.sha for item in current]
    assert _sha(1) not in result.render()


def test_g03_38_46_single_code_pass_and_gap_are_unambiguous() -> None:
    passed = evaluate_review_gate(19, FakeReviewGateData(_one_code()), now=lambda: NOW)
    blocked = evaluate_review_gate(
        19,
        FakeReviewGateData(_one_code(), reviews=()),
        now=lambda: NOW,
    )
    assert passed.status == "PASS"
    assert passed.commits[0].classification == "CODE(final)"
    assert blocked.status == "REVIEW_GAP"


def test_g03_38_47_render_is_deterministic_complete_and_auditable() -> None:
    commits = _chain(
        (
            (_sha(1), (ReviewFile("src/a.py"),), False),
            (_sha(2), (ReviewFile("docs/evidence/a"),), False),
            (_sha(3), (), True),
            (_sha(4), (ReviewFile("src/b.py"),), False),
            (_sha(5), (ReviewFile("src/c.py"),), False),
        )
    )
    reviews = (
        _review(commits[0].sha, "approved"),
        _review(commits[-1].sha, "approved", review_id=2),
    )
    result = evaluate_review_gate(19, FakeReviewGateData(commits, reviews), now=lambda: NOW)
    first = result.render()
    second = result.render()
    assert first == second
    assert f"superseded_by={commits[3].sha}" in first
    assert "prior_verdict=approved" in first
    assert "prior_verdict=missing" in first
    assert "CODE(final)" in first
    assert "METADATA" in first
    assert "NOOP" in first
    assert all(len(item.sha) == 40 for item in result.commits)


def test_g03_38_48_design_and_readme_contracts_are_consistent() -> None:
    design = Path("docs/ao-workflow-v2-design.md").read_text(encoding="utf-8")
    readme = Path("scripts/ao/README.md").read_text(encoding="utf-8")
    assert "2026-08-10" in design
    assert "真实业务验收" in design
    assert "NEEDS_ACCEPTANCE" in design
    assert "未审代码提交的接受" not in design
    assert "SUPERSEDED_EXPLICIT" not in design
    for required in (
        "CODE(final)",
        "SUPERSEDED",
        "METADATA",
        "NOOP",
        "base...final_code_sha",
        "previous_filename",
        "numeric",
        "exit `1`",
        "exit `2`",
        "single-commit patch",
    ):
        assert required in readme


def test_g03_38_49_trusted_surface_bootstrap_cannot_self_approve() -> None:
    design = Path("docs/ao-workflow-v2-design.md").read_text(encoding="utf-8")
    readme = Path("scripts/ao/README.md").read_text(encoding="utf-8")
    for document in (design, readme):
        assert "bootstrap" in document
        assert "Coordinator/Agent" in document
        assert "final SHA" in document
    assert "不能批准自身" in design
    assert "cannot use the modified gate to approve itself" in readme


def test_g03_38_51_required_quality_commands_are_declared() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    for target in ("lint:", "typecheck:", "test-unit:", "check-docs:"):
        assert target in makefile


def _one_code() -> tuple[ReviewCommit, ...]:
    return _chain(((_sha(1), (ReviewFile("src/app.py"),), False),))


def _chain(
    specs: tuple[tuple[str, tuple[ReviewFile, ...], bool], ...],
) -> tuple[ReviewCommit, ...]:
    parent_sha = BASE_SHA
    parent_tree = _sha(900)
    commits: list[ReviewCommit] = []
    for index, (sha, files, noop) in enumerate(specs, start=1):
        tree = parent_tree if noop else _sha(1000 + index)
        commits.append(
            ReviewCommit(
                sha=sha,
                parent_sha=parent_sha,
                tree_sha=tree,
                parent_tree_sha=parent_tree,
                files=files,
            )
        )
        parent_sha = sha
        parent_tree = tree
    return tuple(commits)


def _append(
    commits: tuple[ReviewCommit, ...],
    *specs: tuple[str, tuple[ReviewFile, ...], bool],
) -> tuple[ReviewCommit, ...]:
    parent = commits[-1]
    additions: list[ReviewCommit] = []
    parent_sha = parent.sha
    parent_tree = parent.tree_sha
    for index, (sha, files, noop) in enumerate(specs, start=1):
        tree = parent_tree if noop else _sha(2000 + index)
        additions.append(
            ReviewCommit(
                sha=sha,
                parent_sha=parent_sha,
                tree_sha=tree,
                parent_tree_sha=parent_tree,
                files=files,
            )
        )
        parent_sha = sha
        parent_tree = tree
    return commits + tuple(additions)


def _evidence_for(commits: tuple[ReviewCommit, ...]) -> tuple[CumulativeEvidence, ...]:
    code_indexes = [
        index for index, commit in enumerate(commits) if commit.classification == "CODE"
    ]
    if not code_indexes:
        return ()
    final_index = code_indexes[-1]
    paths = _net_changed_paths(commits[: final_index + 1])
    return (
        CumulativeEvidence(
            base_sha=BASE_SHA,
            final_sha=commits[final_index].sha,
            attested_sha=commits[final_index].sha,
            attested_parent_sha=None,
            merge_base_sha=commits[0].parent_sha,
            changed_paths=paths,
            state="success",
            source="github-compare",
            observed_at=NOW,
            observation_id=1,
            issuer_kind="github-server",
            issuer_id=0,
            complete=True,
            commit_count=final_index + 1,
        ),
    )


def _net_changed_paths(commits: tuple[ReviewCommit, ...]) -> tuple[str, ...]:
    active: dict[str, str | None] = {}
    removed_base_paths: set[str] = set()
    for commit in commits:
        if commit.classification != "CODE":
            continue
        for changed_file in commit.files:
            path = changed_file.filename
            if changed_file.status in {"added", "copied"}:
                active[path] = None
            elif changed_file.status in {"modified", "changed"}:
                active.setdefault(path, path)
            elif changed_file.status == "removed":
                origin = active.pop(path, path)
                if origin is not None:
                    removed_base_paths.add(origin)
            elif changed_file.status == "renamed":
                previous = changed_file.previous_filename
                assert previous is not None
                origin = active.pop(previous, previous)
                active[path] = origin

    paths = set(removed_base_paths)
    for path, origin in active.items():
        paths.add(path)
        if origin is not None and origin != path:
            paths.add(origin)
    return tuple(sorted(paths))


def _review(
    sha: str | None,
    verdict: str,
    *,
    review_id: int = 1,
    author_id: int = TRUSTED_REVIEWER_ID,
) -> ReviewRecord:
    assert sha is not None
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


def _successful_ci() -> dict[str, CIObservation]:
    return {
        name: CIObservation(
            name=name,
            state="success",
            source="check",
            observed_at=NOW,
            observation_id=index,
        )
        for index, name in enumerate(("quality", "database", "container"), start=1)
    }


def _ci_observation(state: str, observation_id: int, observed_at: datetime) -> CIObservation:
    return CIObservation(
        name="quality",
        state=state,
        source="check",
        observed_at=observed_at,
        observation_id=observation_id,
    )
