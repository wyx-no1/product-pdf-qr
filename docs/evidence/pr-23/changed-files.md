# Changed files for PR #23

Comparison: `origin/main...363b46c2045d2eff4997c9130e7bcc9d5de1d209` (three-dot)

| Metric | Value |
|---|---:|
| Merge base | `87e6d4afbb5560b490c845bd8630a3cf79cd2a5e` |
| Changed files | 1 |
| Added lines | 9 |
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
git diff origin/main...363b46c2045d2eff4997c9130e7bcc9d5de1d209 -- migrations/ docs/requirements-v1.md docs/requirements-v2.md CLAUDE.md docs/quality-gates-v1.md docs/advisor-protocol-v1.md docs/decision-register-v1.md docs/test-plan-v1.md
```

The complete changed-path list can be reproduced without relying on this file:

```bash
git diff --name-status origin/main...363b46c2045d2eff4997c9130e7bcc9d5de1d209 -- . ':(exclude)docs/evidence/**'
```
