# Validation record for PR #31

Created at: `2026-08-06T07:56:50.117075+00:00`

This file records evidence locations and results, not an evaluation of their
correctness. When a record disagrees with its original source, the original source
is authoritative.

## CI status

| Field | Value |
|---|---|
| Run ID | `31078106537` |
| Code commit | `a5366b6cc8f6e36cd21b5e047b02e1936d7503ed` |
| Status | `completed` |
| Conclusion | `success` |
| URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31078106537 |
| Merge base | `a5366b6cc8f6e36cd21b5e047b02e1936d7503ed` |

> This CI result applies to code commit `a5366b6cc8f6e36cd21b5e047b02e1936d7503ed`, not to the Evidence commit
> that contains this directory. Do not regenerate Evidence to describe its own
> commit. A later code change requires a new successful CI run and a regenerated
> snapshot.


| Job | Conclusion | URL |
|---|---|---|
| container | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31078106537/job/92555415420 |
| database | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31078106537/job/92555393680 |
| quality | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31078106537/job/92555394107 |

CI retrieval:

```bash
gh run view 31078106537 --json jobs
gh run view 31078106537 --log
gh run download 31078106537 --name quality-reports
gh run download 31078106537 --name database-reports
gh run download 31078106537 --name clean-start-evidence
```

## Test evidence

Test result locations are the `quality-reports` and `database-reports` artifacts
from run `31078106537`. Reproduce the repository checks with:

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

- https://github.com/wyx-no1/product-pdf-qr/pull/31#pullrequestreview-4871779689
- https://github.com/wyx-no1/product-pdf-qr/pull/31#pullrequestreview-4871811829

Independent retrieval:

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/31/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/31/reviews
```
