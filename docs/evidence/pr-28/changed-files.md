# Changed files for PR #28

Comparison: `origin/main...668b7b1842941c5bdc2c85fbc348bd724670e87d` (three-dot)

| Metric | Value |
|---|---:|
| Merge base | `668b7b1842941c5bdc2c85fbc348bd724670e87d` |
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
git diff origin/main...668b7b1842941c5bdc2c85fbc348bd724670e87d -- migrations/ docs/requirements-v1.md docs/requirements-v2.md CLAUDE.md docs/quality-gates-v1.md docs/advisor-protocol-v1.md docs/decision-register-v1.md docs/test-plan-v1.md
```

The complete changed-path list can be reproduced without relying on this file:

```bash
git diff --name-status origin/main...668b7b1842941c5bdc2c85fbc348bd724670e87d -- . ':(exclude)docs/evidence/**'
```
