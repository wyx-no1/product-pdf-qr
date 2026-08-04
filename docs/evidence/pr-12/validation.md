# Validation record for PR #12

Created at: `2026-08-04T00:47:03.913572+00:00`

This file records evidence locations and results, not an evaluation of their
correctness. When a record disagrees with its original source, the original source
is authoritative.

## CI status

| Field | Value |
|---|---|
| Run ID | `30866556420` |
| Code commit | `fbe0bae564f3ed90f8219ca3098812d9b072dcc8` |
| Status | `completed` |
| Conclusion | `success` |
| URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30866556420 |
| Merge base | `af82f581c83bd023b4d17ccc4231a2802acf6f2c` |

> This CI result applies to code commit `fbe0bae564f3ed90f8219ca3098812d9b072dcc8`, not to the Evidence commit
> that contains this directory. Do not regenerate Evidence to describe its own
> commit. A later code change requires a new successful CI run and a regenerated
> snapshot.


| Job | Conclusion | URL |
|---|---|---|
| container | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30866556420/job/91859579737 |
| database | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30866556420/job/91859579694 |
| quality | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30866556420/job/91859579764 |

CI retrieval:

```bash
gh run view 30866556420 --json jobs
gh run view 30866556420 --log
gh run download 30866556420 --name quality-reports
gh run download 30866556420 --name database-reports
gh run download 30866556420 --name clean-start-evidence
```

## Test evidence

Test result locations are the `quality-reports` and `database-reports` artifacts
from run `30866556420`. Reproduce the repository checks with:

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
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3701809654
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3701809960
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3701810267
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3701861914
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3701861918
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3701861922
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3701953765
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3701953979
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3701954184
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3702060367
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3702120876
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3702305149
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3702467145
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3702511752
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3702643152
- https://github.com/wyx-no1/product-pdf-qr/pull/12#discussion_r3702716911

Review record URLs:

- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841244432
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841385709
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841386044
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841386413
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841458182
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841573341
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841573639
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841573924
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841706063
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841782394
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4842024970
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4842246047
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4842303550
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4842469987
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4842570203

Independent retrieval:

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/12/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/12/reviews
```
