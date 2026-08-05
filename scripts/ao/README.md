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
then computes a canonical SHA-256 definition hash from Git tree entries in the
candidate code SHA and the checked-out default branch. The manifest includes:

- all `.github/**` files, including every third-party `uses:` reference and the
  privileged publisher itself;
- `Makefile`, `pyproject.toml`, `uv.lock`, the build/test/link/clean-start scripts,
  and every `tests/**` and `scripts/**` file;
- `Dockerfile`, `.dockerignore`, `.env.example`, Compose files, all `docker/**`
  helpers, `alembic.ini`, and the Alembic environment/template;
- absent-or-present markers for auto-discovered override files such as
  `.coveragerc`, `.coveragerc.toml`, all root pytest config names, `mypy.ini`,
  `setup.cfg`, `tox.ini`, `uv.toml`, Compose overrides, and Dockerfile-specific
  ignore files.

Each entry records path, Git mode, object type, and blob SHA; additions, removals,
renames, permission changes, and content changes all alter the definition hash. The
fixed files are every direct repository-controlled CI command/configuration input.
The recursive roots cover executable test, AO, workflow/action, and Docker helper
code, including newly added files. Product source, migration revisions, and reviewed
documents remain the subjects being tested rather than the gate definition.

### Trusted Execution Surface

The Trusted Execution Surface is the repository-controlled execution and
configuration surface that can influence a gate's result. It is not every file name
that a tool could conceivably discover under a different invocation or future
configuration. Gate commands, their active configuration, executable helpers,
startup hooks, and the configuration that determines discovery boundaries belong to
the surface; ordinary files that cannot enter the current gate execution path do
not.

Both CI Ruff invocations pass `--config pyproject.toml`, which makes that hashed root
file the configuration for every analyzed path and disables Ruff's otherwise
hierarchical closest-config discovery. A candidate `src/ruff.toml`,
`migrations/.ruff.toml`, or nested `pyproject.toml` therefore cannot weaken lint or
format without first changing the hashed Makefile command. As defense in depth, the
definition builder also scans the entire Git tree—not a directory allowlist—for
every `ruff.toml`, `.ruff.toml`, and `pyproject.toml` basename. An attempted nested
override is therefore marked `requires-re-review` even though the pinned commands
make it ineffective. Because this scan is basename-based over the complete candidate
commit, a matching file added under any future directory is covered automatically.

### Execution-path boundary

Membership in the Trusted Execution Surface follows the gate's actual execution
path, not an enumeration of every hypothetical location where a configuration or
plugin file might be placed. Hypothetical locations are unbounded: arbitrary present
and future directories could contain a recognized name. Actual execution paths are
bounded and mechanically verifiable. Hashing files that cannot enter those paths
would make ordinary product changes continually require manual review and would
degrade the mechanism into an always-manual gate without increasing protection.

This rule is safe only because the configuration that selects each execution path is
itself part of the Trusted Execution Surface. If a discovery boundary could be
changed without changing the trusted definition hash, limiting coverage to the
current path would be a bypassable assumption. For pytest, the discovery boundary is
set by `testpaths` in `pyproject.toml`; that file is covered both by the root exact
manifest and the full-tree `pyproject.toml` scan. A PR therefore cannot silently
widen pytest discovery while preserving the trusted hash: changing `testpaths`
produces `requires-re-review`.

Python startup hooks have different loading semantics. `uv sync` installs the project
editably, so an otherwise ordinary `src/sitecustomize.py` can execute when the
interpreter starts, before pytest or mypy processes their own discovery settings.
Every `sitecustomize` and `usercustomize` module form is therefore hashed across the
full tree: the `.py` basenames, extension/bytecode-style basenames, and all files
below same-named package directories. This is path-wide rather than limited to
today's `src/` layout; if the hashed project configuration later changes the editable
source root, the hook scan still covers the new directory.

### Current pytest discovery boundary

Pytest is invoked from the repository root with `-c pyproject.toml`, and the trusted
configuration fixes `testpaths = ["tests"]`. The complete `tests/**` tree and the
root `conftest.py` sentinel are in the trusted definition. A `conftest.py` under
`src/`, migrations, or another product directory is outside that discovery path and
is not loaded by the current pytest gate. If `testpaths` changes in the future, the
trusted `pyproject.toml` changes too, so the candidate is marked
`requires-re-review` before the expanded discovery path can be trusted.

`sitecustomize` and `conftest` are intentionally handled differently because their
loaders are different, not because they are held to different trust standards.
Interpreter startup can load `sitecustomize` independently of pytest's `testpaths`,
so startup hooks need the recursive module-name coverage above. Pytest loads
`conftest` only within its configured discovery boundary, so the trusted root
sentinel and recursive `tests/**` tree cover the actual path without an unbounded
repository-wide `conftest.py` scan.

Other discovery surfaces are bounded as follows:

- all other pytest 9 root config names are represented by missing-file sentinels as
  defense in depth;
- mypy selects one configuration from the invocation directory rather than one per
  analyzed file; `--config-file pyproject.toml` pins it, and every supported root
  candidate (`mypy.ini`, `.mypy.ini`, `pyproject.toml`, and `setup.cfg`) is hashed;
- coverage likewise selects one root configuration (`.coveragerc`, `setup.cfg`,
  `tox.ini`, `.coveragerc.toml`, or `pyproject.toml`) for this invocation;
  `--cov-config=pyproject.toml` pins it and all alternatives are represented by
  exact entries or missing-file sentinels;
- `uv sync` runs at the repository root, so its project/config discovery is anchored
  to the hashed root `pyproject.toml`, `uv.toml`, and `uv.lock`; it does not select a
  separate project configuration for each installed source file.

Generation and both verification paths also require this recomputed default-branch
hash to equal the hash produced by the separate trusted checkout, preventing a
candidate-supplied hash or a changing fetch baseline from becoming the trust anchor.

External runner images and upstream implementations behind movable action major
tags remain platform supply-chain risks. Their reference strings are protected by
the workflow hash, and image/action pins already present in the repository are
protected, but this tool does not independently snapshot GitHub-hosted runner images
or resolve movable tags to immutable commits.

Changes to `.github/workflows/ci.yml` intentionally fail this automatic check. That
also applies to every manifest entry above, including legitimate gate maintenance.
Evidence records the trusted hash, candidate hash, and `requires-re-review` status;
trusted skip is forbidden. A maintainer must review the trust-root change, verify
the latest-head checks, and use the repository's explicit human
bootstrap/branch-protection override process. The publisher does not downgrade the
mismatch, create a trusted success status, or silently adopt the candidate
definition. After the reviewed definition reaches the default branch, a restored or
later matching PR is trusted again. PR #12 itself changes workflow/AO trust-root
files, so its expected self-assessment is `requires-re-review`; it has no exemption.

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

## Review integrity gate

The repository-side merge gate audits every commit in a pull request:

```bash
python -m scripts.ao review-gate --pr 123
```

A commit is metadata, and therefore exempt from CI and review, only when every
changed path is below `docs/evidence/`. Every other commit must have successful
`quality`, `database`, and `container` Checks (with Commit Statuses as a fallback)
and at least one GitHub PR review whose `commit_id` exactly equals the commit SHA.
The final code commit must have an approved verdict.

AO submits reviews with GitHub state `COMMENTED`, so that state is not treated as a
verdict. A verdict is accepted only from a configured trusted GitHub numeric user ID;
the CI workflow pins that ID in its reviewed definition. When the command is run
without `--trusted-reviewer-id`, it retrieves and trusts the repository owner's
server-authenticated numeric ID. Review records retain GitHub's author ID, login,
type, and author association for audit.

The auditable verdict source is exactly one Markdown heading in a submitted,
non-dismissed, trusted review body:

```markdown
## Review verdict: approved
```

or:

```markdown
## Review verdict: changes requested
```

When a commit has multiple trusted bodies with a valid heading, the latest submitted
review is selected deterministically by `submitted_at` and then GitHub review ID.
The parser first collects every `## Review verdict:` heading in a body and accepts
only one complete known value. An absent, unknown, or ambiguous heading is a
`REVIEW_GAP`; it is never inferred as approval. Exact-SHA trusted review activity
that remains without a parseable verdict for more than 30 minutes is additionally
reported as `STALLED`, without triggering a retry. The command exits `0` only for
`PASS`, `2` for `REVIEW_GAP`, and `1` when required GitHub evidence cannot be
retrieved or validated.

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
  --record /absolute/path/to/advisor-record.json \
  --log /absolute/path/to/advisor-validation-events.jsonl
```

The Resolver record binds the opinion to:

- the PR number, Evidence commit, Evidence metadata path, and Evidence code commit;
- the actual reviewed commit;
- a detached workspace lifecycle containing its absolute path, commit, PR,
  timezone-qualified creation and destruction times, and destruction reason
  (`normal`, `timeout`, or `exception`).

Validation accepts an opinion only when the bound commit passes the existing full
Evidence verification in an isolated detached worktree. That verification requires
exactly the five Evidence files, reconstructs their contents from the PR, CI, review,
diff, and trusted-definition facts, and requires the exact server-authenticated
`AO / evidence-snapshot` attestation. A metadata-only, incomplete, author-created,
or unattested commit is therefore rejected. The commit must also be the direct child
of the bound code commit and remain in current PR history.

The reviewed SHA and lifecycle SHA must both equal the Evidence code SHA. The
lifecycle must be complete and self-consistent, the recorded workspace must use the
Resolver naming contract and must no longer be registered or present, and a
default/current repository workspace is always rejected. A current PR head may
differ from the reviewed SHA only through `docs/evidence/**`; any later code commit
invalidates the opinion. The currently bound Evidence Snapshot itself must be the
current PR head so its trusted attestation and remote binding can be reverified.

Every rejection is `indeterminate`, never warning or neutral. The JSON result names
the failed check and reason, and the validator appends the same information to the
validation event log. Only a result with `valid: true` and `gate_status: valid` may
enter G-10.

### Advisor record trust boundary

This first version can mechanically verify that the reviewed code SHA, Evidence
binding, current PR code, and lifecycle fields agree. It prevents process mistakes
such as reviewing the default workspace, reading `main`, or accepting an opinion for
the wrong code version.

It cannot prove that the workspace was actually created by the Resolver. The
lifecycle record is produced locally and has no external notary, cryptographic
signature, or independently issued attestation; a repository writer can fabricate a
self-consistent record. This mechanism therefore does not defend against deliberate
forgery by someone with repository write access. Unforgeable Resolver attestation,
identity signing, and stronger provenance are separate future enhancements.

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
