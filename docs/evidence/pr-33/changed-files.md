# Changed files for PR #33

Comparison: `origin/main...94f825c39aebd2a26b457947b6c31a7812148f3d` (three-dot)

| Metric | Value |
|---|---:|
| Merge base | `bd99824380af984162604fdcdffebe55f3456a35` |
| Changed files | 1 |
| Added lines | 2 |
| Deleted lines | 0 |

## Added files

- None
## Modified files

- `docs/requirements-v2.md`
## Deleted files

- None

## Unmodified-boundary verification

Run the following command from any worktree for this repository. It must produce no
output; any output means a protected boundary changed:

`docs/requirements-v2.md` is intentionally excluded from this unmodified boundary
because Issue #32 explicitly authorizes this PR to change it; the complete changed-path
list and `diff.patch` below remain the authoritative checks for that authorized change.

```bash
git diff origin/main...94f825c39aebd2a26b457947b6c31a7812148f3d -- migrations/ docs/requirements-v1.md CLAUDE.md docs/quality-gates-v1.md docs/advisor-protocol-v1.md docs/decision-register-v1.md docs/test-plan-v1.md
```

The complete changed-path list can be reproduced without relying on this file:

```bash
git diff --name-status origin/main...94f825c39aebd2a26b457947b6c31a7812148f3d -- . ':(exclude)docs/evidence/**'
```
