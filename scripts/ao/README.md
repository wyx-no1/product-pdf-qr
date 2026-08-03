# AO Evidence and Advisor workspace tools

These repository-local Python 3.12 tools implement the two parts of AO P0-1. They
live under `scripts/ao/` so every PR worktree can read them, while setuptools still
packages only `src/` and the runtime Docker stage does not copy `scripts/`.

## Evidence workflow

After a PR's code commit has passed the complete `CI` workflow, run this from the
clean PR branch:

```bash
uv run python -m scripts.ao evidence --repo . --pr 123
```

The command fetches the PR branches, verifies that local HEAD is the exact GitHub PR
head, requires the latest CI run for that SHA and every returned job to be
successful, writes exactly five files under `docs/evidence/pr-123/`, and creates the
separate `docs: add PR123 review evidence` commit. It then pushes that commit to the
same PR branch with a normal non-force push. A concurrent PR update makes the push
fail and leaves the gate indeterminate instead of overwriting newer code.

Generation uses `origin/<base>...<code-sha>` and excludes all of
`docs/evidence/**`. Failure exits nonzero, records an `indeterminate` gate event in
the repository's common Git directory at `ao/evidence-events.jsonl`, and does not
enter review.

## Advisor workspace workflow

The commit argument is intentionally absent. The resolver reads it only from the
Evidence metadata:

```bash
uv run python -m scripts.ao advisor-run \
  --repo /absolute/path/to/repository \
  --metadata /absolute/path/to/docs/evidence/pr-123/metadata.md \
  --record /absolute/path/to/advisor-record.json \
  -- advisor-command --its-arguments
```

The resolver unconditionally fetches first, creates a uniquely named detached
worktree containing the PR number and SHA prefix, exports the bound SHA to the
Advisor process, and removes/prunes the worktree in a `finally` path. SIGINT,
SIGTERM, and SIGHUP are converted into cleanup-capable interruptions. Fetch or
checkout failure pauses review; there is no fallback to `main` or another commit.

Validate the resulting record before gate use:

```bash
uv run python -m scripts.ao advisor-validate \
  --repo /absolute/path/to/repository \
  --metadata /absolute/path/to/docs/evidence/pr-123/metadata.md \
  --record /absolute/path/to/advisor-record.json
```

The record is valid only when the Advisor completed at the Evidence SHA and the PR
has no later non-Evidence code changes.

## Stale worktrees

Detection is read-only and recognizes both current AO names and the legacy
`advisor`/`g01` names that motivated this tool:

```bash
uv run python -m scripts.ao stale-list --repo . --older-than-hours 24
```

Reclamation requires an explicit command and confirmation flag:

```bash
uv run python -m scripts.ao stale-reclaim \
  --repo . --older-than-hours 24 --confirm
```

Only registered Git worktrees inside the configured temporary root whose names
match the known Advisor patterns are eligible. A live PID marker prevents an active
modern workspace from being reported as stale.
