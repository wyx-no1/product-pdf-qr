# Validation record for PR #23

Created at: `2026-08-05T11:50:01.760054+00:00`

This file records evidence locations and results, not an evaluation of their
correctness. When a record disagrees with its original source, the original source
is authoritative.

## CI status

| Field | Value |
|---|---|
| Run ID | `31001869414` |
| Code commit | `363b46c2045d2eff4997c9130e7bcc9d5de1d209` |
| Status | `completed` |
| Conclusion | `success` |
| URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31001869414 |
| Merge base | `87e6d4afbb5560b490c845bd8630a3cf79cd2a5e` |

> This CI result applies to code commit `363b46c2045d2eff4997c9130e7bcc9d5de1d209`, not to the Evidence commit
> that contains this directory. Do not regenerate Evidence to describe its own
> commit. A later code change requires a new successful CI run and a regenerated
> snapshot.


| Job | Conclusion | URL |
|---|---|---|
| container | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31001869414/job/92296178143 |
| database | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31001869414/job/92296205579 |
| quality | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31001869414/job/92296178464 |

CI retrieval:

```bash
gh run view 31001869414 --json jobs
gh run view 31001869414 --log
gh run download 31001869414 --name quality-reports
gh run download 31001869414 --name database-reports
gh run download 31001869414 --name clean-start-evidence
```

## Test evidence

Test result locations are the `quality-reports` and `database-reports` artifacts
from run `31001869414`. Reproduce the repository checks with:

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

- https://github.com/wyx-no1/product-pdf-qr/pull/23#pullrequestreview-4863915053
- https://github.com/wyx-no1/product-pdf-qr/pull/23#pullrequestreview-4863970384

Independent retrieval:

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/23/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/23/reviews
```
