# Changed files for PR #33

Comparison: `origin/main...30f3f2fa01f84a1ecdb8899c7045a7b3f0382dd4` (three-dot)

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

```bash
git diff origin/main...30f3f2fa01f84a1ecdb8899c7045a7b3f0382dd4 -- migrations/ docs/requirements-v1.md docs/requirements-v2.md CLAUDE.md docs/quality-gates-v1.md docs/advisor-protocol-v1.md docs/decision-register-v1.md docs/test-plan-v1.md
```

The complete changed-path list can be reproduced without relying on this file:

```bash
git diff --name-status origin/main...30f3f2fa01f84a1ecdb8899c7045a7b3f0382dd4 -- . ':(exclude)docs/evidence/**'
```
