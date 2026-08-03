# AO Evidence and Advisor workspace tools

These repository-local Python 3.12 tools implement the two parts of AO P0-1. They
live under `scripts/ao/` so every PR worktree can read them, while setuptools still
packages only `src/` and the runtime Docker stage does not copy `scripts/`.

## Evidence workflow

`.github/workflows/ci.yml` runs PR-controlled code with read-only permissions and
non-persistent checkout credentials. After its `quality`, `database`, and
`container` jobs succeed, the separate `.github/workflows/evidence-publish.yml`
workflow is triggered by `workflow_run` and automatically runs:

```bash
python -m scripts.ao evidence --repo . --pr 123 --ci-run-id 456
```

The privileged publisher is loaded from and checks out the default branch as
`trusted/`. It loads Python only from that checkout. The candidate PR checkout is a
separate `candidate/` data directory; no module, script, action, or hook from it is
executed with the write token. Both checkouts use `persist-credentials: false`, and
the write token exists only in the trusted publish step.

Before treating a successful source run as CI evidence, the publisher retrieves the
run's workflow path from GitHub and requires exactly `.github/workflows/ci.yml`. It
then compares that file's complete Git tree entry at the candidate code SHA with the
entry in the checked-out default branch: mode `100644`, object type `blob`, and blob
SHA must all match. Job names and the run conclusion are considered only after this
definition check. A PR therefore cannot substitute same-named no-op jobs or add a
different workflow named `CI`.

Changes to `.github/workflows/ci.yml` intentionally fail this automatic check. That
includes legitimate gate maintenance: a maintainer must review it as a trust-root
change, generate Evidence manually, verify the latest-head checks, and use the
repository's explicit human bootstrap/branch-protection override process. The
publisher does not downgrade the mismatch, create a trusted success status, or
silently adopt the candidate definition. After the reviewed workflow reaches the
default branch, later PRs are compared against that new trusted blob.

The command fetches the PR branches, requires the exact completed three-job CI run,
writes exactly five files under `docs/evidence/pr-123/`, creates a separate
`docs: add PR123 review evidence` commit, and pushes it normally. A concurrent PR
update makes the push fail instead of overwriting newer code.

Before publishing a successful `AO / evidence-snapshot` status on the new head, the
trusted verifier proves that the commit changes exactly those five regular Evidence
files and that the remote PR head is that exact commit. It reconstructs
`metadata.md`, `changed-files.md`, `diff.patch`, `validation.md`, and
`advisor-context.md` from the bound parent, three-dot Git diff, current PR metadata,
the exact successful CI run, and scoped GitHub review records. The creation time is
the only reference-only metadata value: it must be a timezone-qualified ISO-8601
value and is reused while reconstructing both files that record it. All binding,
merge-base, change-count, file-list, patch, CI, job, branch, URL, and Advisor
instructions are compared to independently retrieved facts. The status description
names the parent SHA and source run. Failure posts a failure status and records an
indeterminate event; there is no warning, neutral, or unconditional green path.

A `GITHUB_TOKEN` PR update may create an approval-required, zero-job run. That record
is neither success nor test failure and remains indeterminate. The independent
content-based skip keeps the publisher finite if that run is approved or the
credential later changes to a PAT or deploy key. Path shape only identifies a skip
candidate; before returning `skipped`, the generator itself runs the complete
verification above and requires a pre-existing successful `AO / evidence-snapshot`
status on the exact immutable SHA. Generator identity comes from the GitHub Status
API's server-authenticated creator object: numeric account ID `41898282`, login
`github-actions[bot]`, and type `Bot`, plus the exact parent/source-CI description
and repository workflow-run URL. These are not Git committer strings and cannot be
set by PR content or `git config`. The workflow repeats the verification and never
posts success on the skip branch. Therefore an author-supplied, incomplete, or
tampered Evidence-only commit fails closed.

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
  --timeout-seconds 1800 \
  -- advisor-command --its-arguments
```

The resolver unconditionally fetches first, creates a uniquely named detached
worktree containing the PR number and SHA prefix, exports the bound SHA to the
Advisor process, and removes/prunes the worktree in a `finally` path. SIGINT,
SIGTERM, and SIGHUP are converted into cleanup-capable interruptions. A timeout
terminates the entire Advisor process group, escalates to SIGKILL when it ignores
SIGTERM, confirms exit, then removes the worktree and releases the lock before
recording an indeterminate result. Fetch or checkout failure pauses review; there
is no fallback to `main` or another commit.

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
match the known Advisor patterns are eligible. The wrapper PID, Advisor PID, and
Advisor process group are all checked; any live marker prevents an active modern
workspace from being reported as stale.
