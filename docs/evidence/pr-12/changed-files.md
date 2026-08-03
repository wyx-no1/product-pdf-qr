# Changed files for PR #12

Comparison: `origin/main...57180d81c1411c569797724e777b5d2ba912bb1e` (three-dot)

| Metric | Value |
|---|---:|
| Merge base | `af82f581c83bd023b4d17ccc4231a2802acf6f2c` |
| Changed files | 20 |
| Added lines | 4787 |
| Deleted lines | 0 |

## Added files

- `.github/workflows/evidence-publish.yml`
- `scripts/__init__.py`
- `scripts/ao/README.md`
- `scripts/ao/__init__.py`
- `scripts/ao/__main__.py`
- `scripts/ao/cli.py`
- `scripts/ao/evidence.py`
- `scripts/ao/git.py`
- `scripts/ao/github.py`
- `scripts/ao/models.py`
- `scripts/ao/trust.py`
- `scripts/ao/workspace.py`
- `tests/ao/__init__.py`
- `tests/ao/helpers.py`
- `tests/ao/test_evidence.py`
- `tests/ao/test_github.py`
- `tests/ao/test_trust.py`
- `tests/ao/test_workflow.py`
- `tests/ao/test_workspace.py`
## Modified files

- `.github/workflows/ci.yml`
## Deleted files

- None

## Unmodified-boundary verification

Run the following command from any worktree for this repository. It must produce no
output; any output means a protected boundary changed:

```bash
git diff origin/main...57180d81c1411c569797724e777b5d2ba912bb1e -- migrations/ docs/requirements-v1.md docs/requirements-v2.md CLAUDE.md docs/quality-gates-v1.md docs/advisor-protocol-v1.md docs/decision-register-v1.md docs/test-plan-v1.md
```

The complete changed-path list can be reproduced without relying on this file:

```bash
git diff --name-status origin/main...57180d81c1411c569797724e777b5d2ba912bb1e -- . ':(exclude)docs/evidence/**'
```
