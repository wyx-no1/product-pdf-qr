# Validation record for PR #12

Created at: `2026-08-03T06:00:14.407162+00:00`

This file records evidence locations and results, not an evaluation of their
correctness. When a record disagrees with its original source, the original source
is authoritative.

## CI status

| Field | Value |
|---|---|
| Run ID | `30788671105` |
| Code commit | `744641de628ee664ab41e1a52308b7f747734263` |
| Status | `completed` |
| Conclusion | `success` |
| URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30788671105 |
| Merge base | `af82f581c83bd023b4d17ccc4231a2802acf6f2c` |

> This CI result applies to code commit `744641de628ee664ab41e1a52308b7f747734263`, not to the Evidence commit
> that contains this directory. Do not regenerate Evidence to describe its own
> commit. A later code change requires a new successful CI run and a regenerated
> snapshot.

| Job | Conclusion | URL |
|---|---|---|
| database | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30788671105/job/91607455019 |
| quality | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30788671105/job/91607455032 |
| container | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30788671105/job/91607455057 |

CI retrieval:

```bash
gh run view 30788671105 --json jobs
gh run view 30788671105 --log
gh run download 30788671105 --name quality-reports
gh run download 30788671105 --name database-reports
gh run download 30788671105 --name clean-start-evidence
```

## Test evidence

Test result locations are the `quality-reports` and `database-reports` artifacts
from run `30788671105`. Reproduce the repository checks with:

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

- None

Independent retrieval:

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/12/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/12/reviews
```
