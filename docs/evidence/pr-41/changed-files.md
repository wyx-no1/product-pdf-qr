# Changed files for PR #41

Comparison: `origin/main...46f03da3fbc8e55a0227707db5fabbc22380a09d` (three-dot)

| Metric | Value |
|---|---:|
| Merge base | `46f03da3fbc8e55a0227707db5fabbc22380a09d` |
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
git diff origin/main...46f03da3fbc8e55a0227707db5fabbc22380a09d -- migrations/ docs/requirements-v1.md docs/requirements-v2.md CLAUDE.md docs/quality-gates-v1.md docs/advisor-protocol-v1.md docs/decision-register-v1.md docs/test-plan-v1.md
```

The complete changed-path list can be reproduced without relying on this file:

```bash
git diff --name-status origin/main...46f03da3fbc8e55a0227707db5fabbc22380a09d -- . ':(exclude)docs/evidence/**'
```
