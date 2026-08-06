# Validation record for PR #33

Created at: `2026-08-06T08:02:47.270537+00:00`

This file records evidence locations and results, not an evaluation of their
correctness. When a record disagrees with its original source, the original source
is authoritative.

## CI status

| Field | Value |
|---|---|
| Run ID | `31082995505` |
| Code commit | `30f3f2fa01f84a1ecdb8899c7045a7b3f0382dd4` |
| Status | `completed` |
| Conclusion | `success` |
| URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31082995505 |
| Merge base | `bd99824380af984162604fdcdffebe55f3456a35` |

> This CI result applies to code commit `30f3f2fa01f84a1ecdb8899c7045a7b3f0382dd4`, not to the Evidence commit
> that contains this directory. Do not regenerate Evidence to describe its own
> commit. A later code change requires a new successful CI run and a regenerated
> snapshot.


| Job | Conclusion | URL |
|---|---|---|
| container | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31082995505/job/92556730836 |
| database | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31082995505/job/92556708273 |
| quality | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31082995505/job/92556708584 |

CI retrieval:

```bash
gh run view 31082995505 --json jobs
gh run view 31082995505 --log
gh run download 31082995505 --name quality-reports
gh run download 31082995505 --name database-reports
gh run download 31082995505 --name clean-start-evidence
```

## Test evidence

Test result locations are the `quality-reports` and `database-reports` artifacts
from run `31082995505`. Reproduce the repository checks with:

```bash
make build-reproducible
make typecheck
make lint
make test-unit
make test-integration
```

## Reviewer status

GitHub review decision at generation time: `not-reviewed`

Line-level comment URLs:

- None

Review record URLs:

- https://github.com/wyx-no1/product-pdf-qr/pull/33#pullrequestreview-4871753527
- https://github.com/wyx-no1/product-pdf-qr/pull/33#pullrequestreview-4871757677
- https://github.com/wyx-no1/product-pdf-qr/pull/33#pullrequestreview-4872385063

Independent retrieval:

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/33/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/33/reviews
```
