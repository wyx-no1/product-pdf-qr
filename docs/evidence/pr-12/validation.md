# Validation record for PR #12

Created at: `2026-08-03T06:33:20.941685+00:00`

This file records evidence locations and results, not an evaluation of their
correctness. When a record disagrees with its original source, the original source
is authoritative.

## CI status

| Field | Value |
|---|---|
| Run ID | `30790486727` |
| Code commit | `edc067f4836762f0729402e4fc545e708dfaeb43` |
| Status | `required-jobs-completed` |
| Conclusion | `success` |
| URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30790486727 |
| Merge base | `af82f581c83bd023b4d17ccc4231a2802acf6f2c` |

> This CI result applies to code commit `edc067f4836762f0729402e4fc545e708dfaeb43`, not to the Evidence commit
> that contains this directory. Do not regenerate Evidence to describe its own
> commit. A later code change requires a new successful CI run and a regenerated
> snapshot.

The automatic Evidence job observed all three required code jobs as successful. The
workflow status was still active solely because the blocking Evidence job had not yet
finished.

| Job | Conclusion | URL |
|---|---|---|
| database | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30790486727/job/91612764181 |
| container | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30790486727/job/91612764184 |
| quality | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30790486727/job/91612764208 |

CI retrieval:

```bash
gh run view 30790486727 --json jobs
gh run view 30790486727 --log
gh run download 30790486727 --name quality-reports
gh run download 30790486727 --name database-reports
gh run download 30790486727 --name clean-start-evidence
```

## Test evidence

Test result locations are the `quality-reports` and `database-reports` artifacts
from run `30790486727`. Reproduce the repository checks with:

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

- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3701694894
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3701694898
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3701694902

Review record URLs:

- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841244432

Independent retrieval:

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/12/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/12/reviews
```
