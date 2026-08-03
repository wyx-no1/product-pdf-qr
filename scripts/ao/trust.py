from __future__ import annotations

from dataclasses import dataclass

from scripts.ao.git import GitRepository

TRUSTED_CI_WORKFLOW_PATH = ".github/workflows/ci.yml"


class CIWorkflowTrustError(RuntimeError):
    """The source CI run did not use the trusted workflow definition."""


@dataclass(frozen=True)
class VerifiedCIWorkflow:
    path: str
    blob_sha: str
    trusted_commit_sha: str
    candidate_commit_sha: str


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    object_type: str
    object_sha: str
    path: str


def verify_ci_workflow_definition(
    trusted_repository: GitRepository,
    candidate_repository: GitRepository,
    candidate_sha: str,
    source_run_path: str,
) -> VerifiedCIWorkflow:
    """Require the source run to use the default-branch CI workflow byte-for-byte."""
    _require_full_sha(candidate_sha)
    if source_run_path != TRUSTED_CI_WORKFLOW_PATH:
        raise CIWorkflowTrustError(
            f"source CI run path {source_run_path!r} is not trusted {TRUSTED_CI_WORKFLOW_PATH!r}"
        )

    trusted_sha = trusted_repository.git("rev-parse", "HEAD").stdout.strip()
    _require_full_sha(trusted_sha)
    trusted_entry = _workflow_tree_entry(trusted_repository, trusted_sha)
    candidate_entry = _workflow_tree_entry(candidate_repository, candidate_sha)
    if trusted_entry.mode != "100644" or trusted_entry.object_type != "blob":
        raise CIWorkflowTrustError(
            f"trusted {TRUSTED_CI_WORKFLOW_PATH} must be a regular non-executable Git blob"
        )
    if candidate_entry != trusted_entry:
        raise CIWorkflowTrustError(
            f"candidate {TRUSTED_CI_WORKFLOW_PATH} tree entry differs from the "
            "trusted default-branch definition; automatic Evidence publication is blocked"
        )
    return VerifiedCIWorkflow(
        path=TRUSTED_CI_WORKFLOW_PATH,
        blob_sha=trusted_entry.object_sha,
        trusted_commit_sha=trusted_sha,
        candidate_commit_sha=candidate_sha,
    )


def _workflow_tree_entry(repository: GitRepository, commit_sha: str) -> _TreeEntry:
    output = repository.git(
        "ls-tree",
        commit_sha,
        "--",
        TRUSTED_CI_WORKFLOW_PATH,
    ).stdout.rstrip("\n")
    metadata, separator, path = output.partition("\t")
    fields = metadata.split()
    if not separator or len(fields) != 3 or path != TRUSTED_CI_WORKFLOW_PATH:
        raise CIWorkflowTrustError(
            f"commit {commit_sha} lacks a single regular {TRUSTED_CI_WORKFLOW_PATH} entry"
        )
    return _TreeEntry(
        mode=fields[0],
        object_type=fields[1],
        object_sha=fields[2],
        path=path,
    )


def _require_full_sha(value: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise CIWorkflowTrustError("candidate CI commit SHA must be a full lowercase SHA")
