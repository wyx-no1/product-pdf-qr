from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PullRequest:
    number: int
    repository: str
    title: str
    source_branch: str
    target_branch: str
    head_sha: str
    url: str
    review_decision: str


@dataclass(frozen=True)
class CIJob:
    name: str
    conclusion: str
    url: str


@dataclass(frozen=True)
class CIRun:
    run_id: int
    commit_sha: str
    status: str
    conclusion: str
    url: str
    jobs: tuple[CIJob, ...]


@dataclass(frozen=True)
class ReviewEvidence:
    decision: str
    line_comment_urls: tuple[str, ...]
    review_urls: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceBinding:
    pr_number: int
    source_branch: str
    target_branch: str
    code_commit_sha: str
    ci_run_id: int
    ci_conclusion: str


@dataclass(frozen=True)
class EvidenceSkip:
    head_sha: str
    reason: str


@dataclass(frozen=True)
class EvidenceAttestation:
    context: str
    state: str
    creator_login: str
    description: str
    target_url: str
