# Validation record for PR #12

Created at: `2026-08-03T09:06:39.903010+00:00`

This file records evidence locations and results, not an evaluation of their
correctness. When a record disagrees with its original source, the original source
is authoritative.

## CI status

| Field | Value |
|---|---|
| Run ID | `30799823655` |
| Code commit | `811693afe7e6de4956f5925f830f5e3d75ee3e15` |
| Status | `completed` |
| Conclusion | `success` |
| URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30799823655 |
| Merge base | `af82f581c83bd023b4d17ccc4231a2802acf6f2c` |

> This CI result applies to code commit `811693afe7e6de4956f5925f830f5e3d75ee3e15`, not to the Evidence commit
> that contains this directory. Do not regenerate Evidence to describe its own
> commit. A later code change requires a new successful CI run and a regenerated
> snapshot.


| Job | Conclusion | URL |
|---|---|---|
| container | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30799823655/job/91641593683 |
| database | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30799823655/job/91641593719 |
| quality | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30799823655/job/91641593845 |

CI retrieval:

```bash
gh run view 30799823655 --json jobs
gh run view 30799823655 --log
gh run download 30799823655 --name quality-reports
gh run download 30799823655 --name database-reports
gh run download 30799823655 --name clean-start-evidence
```

## Test evidence

Test result locations are the `quality-reports` and `database-reports` artifacts
from run `30799823655`. Reproduce the repository checks with:

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

Independent retrieval:

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/12/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/12/reviews
```
