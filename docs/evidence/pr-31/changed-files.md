# Changed files for PR #31

Comparison: `origin/main...a5366b6cc8f6e36cd21b5e047b02e1936d7503ed` (three-dot)

| Metric | Value |
|---|---:|
| Merge base | `a5366b6cc8f6e36cd21b5e047b02e1936d7503ed` |
| Changed files | 0 |
| Added lines | 0 |
| Deleted lines | 0 |

## Added files

- None
## Modified files

- None
## Deleted files

- None

## Unmodified-boundary verification

Run the following command from any worktree for this repository. It must produce no
output; any output means a protected boundary changed:

```bash
git diff origin/main...a5366b6cc8f6e36cd21b5e047b02e1936d7503ed -- migrations/ docs/requirements-v1.md docs/requirements-v2.md CLAUDE.md docs/quality-gates-v1.md docs/advisor-protocol-v1.md docs/decision-register-v1.md docs/test-plan-v1.md
```

The complete changed-path list can be reproduced without relying on this file:

```bash
git diff --name-status origin/main...a5366b6cc8f6e36cd21b5e047b02e1936d7503ed -- . ':(exclude)docs/evidence/**'
```
