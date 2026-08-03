from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath

from scripts.ao.git import GitRepository

TRUSTED_CI_WORKFLOW_PATH = ".github/workflows/ci.yml"

# These fixed files are direct gate commands/configuration or auto-discovered
# configuration that could override the checked-in policy when newly added. Ruff
# is explicitly pinned to the root pyproject.toml by these hashed Makefile commands,
# so its otherwise hierarchical per-file configuration discovery is disabled.
TRUSTED_CI_EXACT_PATHS = (
    ".coveragerc",
    ".coveragerc.toml",
    ".dockerignore",
    ".env.example",
    ".mypy.ini",
    ".pytest.ini",
    ".pytest.toml",
    ".python-version",
    ".ruff.toml",
    "Dockerfile",
    "Dockerfile.dockerignore",
    "GNUmakefile",
    "MANIFEST.in",
    "Makefile",
    "alembic.ini",
    "compose.override.yml",
    "compose.override.yaml",
    "compose.yaml",
    "compose.yml",
    "conftest.py",
    "docker-compose.override.yaml",
    "docker-compose.override.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
    "makefile",
    "migrations/env.py",
    "migrations/script.py.mako",
    "mypy.ini",
    "pyproject.toml",
    "pytest.ini",
    "pytest.toml",
    "ruff.toml",
    "setup.cfg",
    "setup.py",
    "sitecustomize.py",
    "tox.ini",
    "usercustomize.py",
    "uv.lock",
    "uv.toml",
)

# Every Git entry below these roots is part of the gate implementation. New,
# removed, renamed, or mode-changed files therefore alter the definition hash.
TRUSTED_CI_TREE_PATHS = (
    ".github",
    "docker",
    "scripts",
    "tests",
)

# Ruff is structurally pinned to the root pyproject.toml in Makefile. Scan the
# complete Git tree as defense in depth so an attempted nested closest-config
# override is still an explicit trust-root change, including in future directories.
TRUSTED_CI_RECURSIVE_FILE_NAMES = frozenset(
    {
        ".ruff.toml",
        "pyproject.toml",
        "ruff.toml",
    }
)


class CIWorkflowTrustError(RuntimeError):
    """The source CI run did not use a comparable trusted workflow definition."""


@dataclass(frozen=True)
class CIDefinitionComparison:
    source_run_path: str
    trusted_definition_hash: str
    candidate_definition_hash: str
    status: str
    differing_paths: tuple[str, ...]
    trusted_commit_sha: str
    candidate_commit_sha: str


def compare_ci_definition(
    trusted_repository: GitRepository,
    candidate_repository: GitRepository,
    candidate_sha: str,
    source_run_path: str,
    *,
    trusted_ref: str = "HEAD",
) -> CIDefinitionComparison:
    """Compare every repository-controlled CI gate input against a trusted ref."""
    _require_full_sha(candidate_sha, "candidate CI commit SHA")
    if source_run_path != TRUSTED_CI_WORKFLOW_PATH:
        raise CIWorkflowTrustError(
            f"source CI run path {source_run_path!r} is not trusted {TRUSTED_CI_WORKFLOW_PATH!r}"
        )

    trusted_sha = trusted_repository.git("rev-parse", trusted_ref).stdout.strip()
    _require_full_sha(trusted_sha, "trusted CI commit SHA")
    trusted_entries = _definition_entries(trusted_repository, trusted_sha)
    candidate_entries = _definition_entries(candidate_repository, candidate_sha)
    differing_paths = tuple(
        path
        for path in sorted(trusted_entries.keys() | candidate_entries.keys())
        if trusted_entries.get(path) != candidate_entries.get(path)
    )
    trusted_hash = _definition_hash(trusted_entries)
    candidate_hash = _definition_hash(candidate_entries)
    status = (
        "trusted"
        if not differing_paths and trusted_hash == candidate_hash
        else "requires-re-review"
    )
    return CIDefinitionComparison(
        source_run_path=source_run_path,
        trusted_definition_hash=trusted_hash,
        candidate_definition_hash=candidate_hash,
        status=status,
        differing_paths=differing_paths,
        trusted_commit_sha=trusted_sha,
        candidate_commit_sha=candidate_sha,
    )


def _definition_entries(repository: GitRepository, commit_sha: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for path in TRUSTED_CI_EXACT_PATHS:
        entry = _single_tree_entry(repository, commit_sha, path)
        entries[path] = entry or "missing"
    all_files = repository.git("ls-tree", "-r", commit_sha, "--", ".").stdout
    for line in all_files.splitlines():
        path, value = _parse_tree_entry(line)
        if PurePosixPath(path).name in TRUSTED_CI_RECURSIVE_FILE_NAMES:
            _record_entry(entries, path, value)
    for root in TRUSTED_CI_TREE_PATHS:
        output = repository.git("ls-tree", "-r", commit_sha, "--", root).stdout
        for line in output.splitlines():
            path, value = _parse_tree_entry(line)
            _record_entry(entries, path, value)
    return entries


def _record_entry(entries: dict[str, str], path: str, value: str) -> None:
    existing = entries.get(path)
    if existing is not None and existing != value:
        raise CIWorkflowTrustError(
            f"trusted CI definition path has conflicting Git entries: {path}"
        )
    entries[path] = value


def _single_tree_entry(
    repository: GitRepository,
    commit_sha: str,
    path: str,
) -> str | None:
    output = repository.git("ls-tree", commit_sha, "--", path).stdout.rstrip("\n")
    if not output:
        return None
    parsed_path, value = _parse_tree_entry(output)
    if parsed_path != path:
        raise CIWorkflowTrustError(
            f"commit {commit_sha} returned unexpected tree path {parsed_path!r} for {path!r}"
        )
    return value


def _parse_tree_entry(line: str) -> tuple[str, str]:
    metadata, separator, path = line.partition("\t")
    fields = metadata.split()
    if not separator or len(fields) != 3 or not path:
        raise CIWorkflowTrustError(f"malformed trusted CI Git tree entry: {line!r}")
    mode, object_type, object_sha = fields
    if object_type != "blob":
        raise CIWorkflowTrustError(f"trusted CI definition entry must be a blob: {path}")
    return path, f"{mode} {object_type} {object_sha}"


def _definition_hash(entries: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path, value in sorted(entries.items()):
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(value.encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _require_full_sha(value: str, label: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise CIWorkflowTrustError(f"{label} must be a full lowercase SHA")
