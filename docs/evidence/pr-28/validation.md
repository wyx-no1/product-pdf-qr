# Validation record for PR #28

Created at: `2026-08-06T04:41:47.815219+00:00`

This file records evidence locations and results, not an evaluation of their
correctness. When a record disagrees with its original source, the original source
is authoritative.

## CI status

| Field | Value |
|---|---|
| Run ID | `31068165349` |
| Code commit | `668b7b1842941c5bdc2c85fbc348bd724670e87d` |
| Status | `completed` |
| Conclusion | `success` |
| URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31068165349 |
| Merge base | `668b7b1842941c5bdc2c85fbc348bd724670e87d` |

> This CI result applies to code commit `668b7b1842941c5bdc2c85fbc348bd724670e87d`, not to the Evidence commit
> that contains this directory. Do not regenerate Evidence to describe its own
> commit. A later code change requires a new successful CI run and a regenerated
> snapshot.


| Job | Conclusion | URL |
|---|---|---|
| container | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31068165349/job/92521340808 |
| database | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31068165349/job/92521340842 |
| quality | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31068165349/job/92521340837 |

CI retrieval:

```bash
gh run view 31068165349 --json jobs
gh run view 31068165349 --log
gh run download 31068165349 --name quality-reports
gh run download 31068165349 --name database-reports
gh run download 31068165349 --name clean-start-evidence
```

## Test evidence

Test result locations are the `quality-reports` and `database-reports` artifacts
from run `31068165349`. Reproduce the repository checks with:

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

- https://github.com/wyx-no1/product-pdf-qr/pull/28#pullrequestreview-4864247156
- https://github.com/wyx-no1/product-pdf-qr/pull/28#pullrequestreview-4864266072
- https://github.com/wyx-no1/product-pdf-qr/pull/28#pullrequestreview-4870532430
- https://github.com/wyx-no1/product-pdf-qr/pull/28#pullrequestreview-4870652463
- https://github.com/wyx-no1/product-pdf-qr/pull/28#pullrequestreview-4870672872

Independent retrieval:

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/28/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/28/reviews
```
