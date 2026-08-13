# Validation record for PR #41

Created at: `2026-08-13T12:29:59.291629+00:00`

This file records evidence locations and results, not an evaluation of their
correctness. When a record disagrees with its original source, the original source
is authoritative.

## CI status

| Field | Value |
|---|---|
| Run ID | `31699966821` |
| Code commit | `46f03da3fbc8e55a0227707db5fabbc22380a09d` |
| Status | `completed` |
| Conclusion | `success` |
| URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31699966821 |
| Merge base | `46f03da3fbc8e55a0227707db5fabbc22380a09d` |

> This CI result applies to code commit `46f03da3fbc8e55a0227707db5fabbc22380a09d`, not to the Evidence commit
> that contains this directory. Do not regenerate Evidence to describe its own
> commit. A later code change requires a new successful CI run and a regenerated
> snapshot.


| Job | Conclusion | URL |
|---|---|---|
| container | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31699966821/job/94446478234 |
| database | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31699966821/job/94446478287 |
| quality | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31699966821/job/94446478403 |

CI retrieval:

```bash
gh run view 31699966821 --json jobs
gh run view 31699966821 --log
gh run download 31699966821 --name quality-reports
gh run download 31699966821 --name database-reports
gh run download 31699966821 --name clean-start-evidence
```

## Test evidence

Test result locations are the `quality-reports` and `database-reports` artifacts
from run `31699966821`. Reproduce the repository checks with:

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

- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765198902
- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765198908
- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765198911
- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765198916
- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765415783
- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765416172
- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765416663
- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765417147
- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765492924
- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765492933
- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765591133
- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765591473
- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765907026
- https://github.com/wyx-no1/product-pdf-qr/pull/41#discussion_r3765957854

Review record URLs:

- https://github.com/wyx-no1/product-pdf-qr/pull/41#pullrequestreview-4915017813
- https://github.com/wyx-no1/product-pdf-qr/pull/41#pullrequestreview-4915286216
- https://github.com/wyx-no1/product-pdf-qr/pull/41#pullrequestreview-4915286745
- https://github.com/wyx-no1/product-pdf-qr/pull/41#pullrequestreview-4915287595
- https://github.com/wyx-no1/product-pdf-qr/pull/41#pullrequestreview-4915288299
- https://github.com/wyx-no1/product-pdf-qr/pull/41#pullrequestreview-4915388863
- https://github.com/wyx-no1/product-pdf-qr/pull/41#pullrequestreview-4915504353
- https://github.com/wyx-no1/product-pdf-qr/pull/41#pullrequestreview-4915504713
- https://github.com/wyx-no1/product-pdf-qr/pull/41#pullrequestreview-4915884661
- https://github.com/wyx-no1/product-pdf-qr/pull/41#pullrequestreview-4915949195
- https://github.com/wyx-no1/product-pdf-qr/pull/41#pullrequestreview-4916032800
- https://github.com/wyx-no1/product-pdf-qr/pull/41#pullrequestreview-4922452908

Independent retrieval:

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/41/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/41/reviews
```
