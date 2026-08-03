# Validation record for PR #12

Created at: `2026-08-03T07:03:44.474517+00:00`

This file records evidence locations and results, not an evaluation of their
correctness. When a record disagrees with its original source, the original source
is authoritative.

## CI status

| Field | Value |
|---|---|
| Run ID | `30792140720` |
| Code commit | `80157fb3ae1e3a955eb0da29557dad80e57bc437` |
| Status | `completed` |
| Conclusion | `success` |
| URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30792140720 |
| Merge base | `af82f581c83bd023b4d17ccc4231a2802acf6f2c` |

> This CI result applies to code commit `80157fb3ae1e3a955eb0da29557dad80e57bc437`, not to the Evidence commit
> that contains this directory. Do not regenerate Evidence to describe its own
> commit. A later code change requires a new successful CI run and a regenerated
> snapshot.


| Job | Conclusion | URL |
|---|---|---|
| database | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30792140720/job/91617738517 |
| container | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30792140720/job/91617738585 |
| quality | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30792140720/job/91617738621 |

CI retrieval:

```bash
gh run view 30792140720 --json jobs
gh run view 30792140720 --log
gh run download 30792140720 --name quality-reports
gh run download 30792140720 --name database-reports
gh run download 30792140720 --name clean-start-evidence
```

## Test evidence

Test result locations are the `quality-reports` and `database-reports` artifacts
from run `30792140720`. Reproduce the repository checks with:

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

Review record URLs:

- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841244432
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841385709
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841386044
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841386413
- https://github.com/wyx-no1/product-pdf-qr/pull/12#pullrequestreview-4841458182

Independent retrieval:

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/12/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/12/reviews
```
