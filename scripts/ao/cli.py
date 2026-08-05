from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from types import FrameType
from typing import Never

from scripts.ao.evidence import EvidenceGenerator, verify_evidence_head
from scripts.ao.git import GitRepository
from scripts.ao.github import GhGitHubData
from scripts.ao.models import EvidenceSkip
from scripts.ao.review_gate import GhReviewGateData, evaluate_review_gate
from scripts.ao.trust import compare_ci_definition
from scripts.ao.workspace import (
    WorkspaceResolver,
    detect_stale_worktrees,
    reclaim_stale_worktrees,
    validate_advisor_opinion,
)

type SignalHandler = Callable[[int, FrameType | None], object] | int | None


class AdvisorInterrupted(RuntimeError):
    """A handled signal interrupted an Advisor command."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.ao")
    commands = parser.add_subparsers(dest="command", required=True)

    verify_ci = commands.add_parser(
        "ci-verify-definition",
        help="verify that source CI used the trusted default-branch workflow definition",
    )
    verify_ci.add_argument("--trusted-repo", type=Path, required=True)
    verify_ci.add_argument("--candidate-repo", type=Path, required=True)
    verify_ci.add_argument("--candidate-sha", required=True)
    verify_ci.add_argument("--source-run-path", required=True)

    evidence = commands.add_parser("evidence", help="generate a bound Evidence Snapshot")
    evidence.add_argument("--repo", type=Path, required=True)
    evidence.add_argument("--pr", type=int, required=True)
    evidence.add_argument("--log", type=Path)
    evidence.add_argument(
        "--ci-run-id",
        type=int,
        help="current workflow run whose required code jobs have completed",
    )
    evidence.add_argument(
        "--trusted-ci-definition-hash",
        help="default-branch CI definition hash computed by the trusted publisher",
    )

    review_gate = commands.add_parser(
        "review-gate",
        help="verify CI and exact-SHA GitHub review coverage for every PR code commit",
    )
    review_gate.add_argument("--repo", type=Path, default=Path("."))
    review_gate.add_argument("--pr", type=int, required=True)
    review_gate.add_argument(
        "--trusted-reviewer-id",
        action="append",
        type=int,
        help="trusted GitHub numeric user ID; repeat to trust multiple reviewers",
    )

    verify_evidence = commands.add_parser(
        "evidence-verify-head",
        help="verify an Evidence-only PR head before publishing its status",
    )
    verify_evidence.add_argument("--repo", type=Path, required=True)
    verify_evidence.add_argument("--pr", type=int, required=True)
    verify_evidence.add_argument("--evidence-sha", required=True)
    verify_evidence.add_argument(
        "--trusted-ci-definition-hash",
        help="default-branch CI definition hash computed by the trusted publisher",
    )
    verify_evidence.add_argument(
        "--require-prior-attestation",
        action="store_true",
        help="allow a skipped Evidence head only when the trusted publisher attested it earlier",
    )

    advisor = commands.add_parser("advisor-run", help="run Advisor in a bound worktree")
    advisor.add_argument("--repo", type=Path, required=True)
    advisor.add_argument("--metadata", type=Path, required=True)
    advisor.add_argument("--record", type=Path, required=True)
    advisor.add_argument("--temp-root", type=Path)
    advisor.add_argument("--log", type=Path)
    advisor.add_argument("--timeout-seconds", type=float, default=1800.0)
    advisor.add_argument("advisor_command", nargs=argparse.REMAINDER)

    validate = commands.add_parser(
        "advisor-validate",
        help="validate an Advisor record against Evidence and current PR code",
    )
    validate.add_argument("--repo", type=Path, required=True)
    validate.add_argument("--metadata", type=Path, required=True)
    validate.add_argument("--record", type=Path, required=True)
    validate.add_argument("--log", type=Path)

    stale = commands.add_parser("stale-list", help="detect stale Advisor worktrees")
    _add_stale_arguments(stale)

    reclaim = commands.add_parser("stale-reclaim", help="reclaim detected stale worktrees")
    _add_stale_arguments(reclaim)
    reclaim.add_argument("--log", type=Path)
    reclaim.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="explicitly authorize removal of the detected temporary worktrees",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "ci-verify-definition":
            return _ci_verify_definition(arguments)
        if arguments.command == "evidence":
            return _evidence(arguments)
        if arguments.command == "evidence-verify-head":
            return _evidence_verify_head(arguments)
        if arguments.command == "review-gate":
            return _review_gate(arguments)
        if arguments.command == "advisor-run":
            return _advisor_run(arguments)
        if arguments.command == "advisor-validate":
            return _advisor_validate(arguments)
        if arguments.command == "stale-list":
            return _stale_list(arguments)
        if arguments.command == "stale-reclaim":
            return _stale_reclaim(arguments)
        raise AssertionError(f"unhandled command {arguments.command}")
    except Exception as error:
        print(
            json.dumps(
                {
                    "error": str(error),
                    "gate_status": "indeterminate",
                    "result": "failure",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


def _evidence(arguments: argparse.Namespace) -> int:
    repository = GitRepository(arguments.repo)
    github = GhGitHubData(repository.remote_slug())
    generator = EvidenceGenerator(
        repository,
        github,
        log_path=arguments.log,
    )
    result = generator.generate(
        arguments.pr,
        ci_run_id=arguments.ci_run_id,
        trusted_ci_definition_hash=arguments.trusted_ci_definition_hash,
    )
    if isinstance(result, EvidenceSkip):
        _print_json(
            {
                "head_sha": result.head_sha,
                "reason": result.reason,
                "result": "skipped",
            }
        )
        return 0
    _print_json(
        {
            "ci_run_id": result.ci_run_id,
            "ci_definition_status": result.ci_definition_status,
            "trusted_ci_definition_hash": result.trusted_ci_definition_hash,
            "candidate_ci_definition_hash": result.candidate_ci_definition_hash,
            "code_commit_sha": result.code_commit_sha,
            "directory": str(result.directory),
            "evidence_commit_sha": result.evidence_commit_sha,
            "result": "success",
        }
    )
    return 0


def _ci_verify_definition(arguments: argparse.Namespace) -> int:
    result = compare_ci_definition(
        GitRepository(arguments.trusted_repo),
        GitRepository(arguments.candidate_repo),
        arguments.candidate_sha,
        arguments.source_run_path,
    )
    _print_json(asdict(result))
    return 0


def _review_gate(arguments: argparse.Namespace) -> int:
    repository = GitRepository(arguments.repo)
    github = GhReviewGateData(repository.remote_slug())
    configured_ids = (
        frozenset(arguments.trusted_reviewer_id)
        if arguments.trusted_reviewer_id is not None
        else None
    )
    result = evaluate_review_gate(
        arguments.pr,
        github,
        trusted_reviewer_ids=configured_ids,
    )
    print(result.render())
    return result.exit_code


def _advisor_run(arguments: argparse.Namespace) -> int:
    command = tuple(arguments.advisor_command)
    if command and command[0] == "--":
        command = command[1:]
    repository = GitRepository(arguments.repo)
    resolver = WorkspaceResolver(
        repository,
        temp_root=arguments.temp_root,
        log_path=arguments.log,
    )
    with _handled_termination_signals():
        return resolver.run_advisor(
            arguments.metadata,
            command,
            arguments.record,
            timeout_seconds=arguments.timeout_seconds,
        )


def _evidence_verify_head(arguments: argparse.Namespace) -> int:
    repository = GitRepository(arguments.repo)
    github = GhGitHubData(repository.remote_slug())
    result = verify_evidence_head(
        repository,
        github,
        arguments.pr,
        arguments.evidence_sha,
        require_prior_attestation=arguments.require_prior_attestation,
        trusted_ci_definition_hash=arguments.trusted_ci_definition_hash,
    )
    _print_json(asdict(result))
    return 0


def _advisor_validate(arguments: argparse.Namespace) -> int:
    repository = GitRepository(arguments.repo)
    github = GhGitHubData(repository.remote_slug())
    result = validate_advisor_opinion(
        repository,
        github,
        arguments.metadata,
        arguments.record,
        log_path=arguments.log,
    )
    _print_json(asdict(result))
    return 0 if result.valid else 2


def _stale_list(arguments: argparse.Namespace) -> int:
    repository = GitRepository(arguments.repo)
    stale = detect_stale_worktrees(
        repository,
        temp_root=arguments.temp_root,
        older_than_seconds=arguments.older_than_hours * 3600,
    )
    _print_json(
        {
            "count": len(stale),
            "worktrees": [
                {
                    **asdict(item),
                    "path": str(item.path),
                }
                for item in stale
            ],
        }
    )
    return 0


def _stale_reclaim(arguments: argparse.Namespace) -> int:
    repository = GitRepository(arguments.repo)
    stale = detect_stale_worktrees(
        repository,
        temp_root=arguments.temp_root,
        older_than_seconds=arguments.older_than_hours * 3600,
    )
    reclaimed = reclaim_stale_worktrees(
        repository,
        stale,
        log_path=arguments.log,
    )
    _print_json(
        {
            "count": len(reclaimed),
            "reclaimed": [str(path) for path in reclaimed],
        }
    )
    return 0


def _add_stale_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--older-than-hours", type=float, default=24.0)


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


@contextmanager
def _handled_termination_signals() -> Iterator[None]:
    previous: dict[signal.Signals, SignalHandler] = {}

    def interrupt(signum: int, frame: FrameType | None) -> Never:
        del frame
        raise AdvisorInterrupted(f"Advisor interrupted by signal {signum}")

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
