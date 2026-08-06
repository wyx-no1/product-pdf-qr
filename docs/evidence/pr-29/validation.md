# Validation record for PR #29

Created at: `2026-08-06T05:24:32.583212+00:00`

This file records evidence locations and results, not an evaluation of their
correctness. When a record disagrees with its original source, the original source
is authoritative.

## CI status

| Field | Value |
|---|---|
| Run ID | `31072076150` |
| Code commit | `a2bc83650846994a6a3124adced28c8444da1aa0` |
| Status | `completed` |
| Conclusion | `success` |
| URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31072076150 |
| Merge base | `a2bc83650846994a6a3124adced28c8444da1aa0` |

> This CI result applies to code commit `a2bc83650846994a6a3124adced28c8444da1aa0`, not to the Evidence commit
> that contains this directory. Do not regenerate Evidence to describe its own
> commit. A later code change requires a new successful CI run and a regenerated
> snapshot.


| Job | Conclusion | URL |
|---|---|---|
| container | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31072076150/job/92527821273 |
| database | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31072076150/job/92527820033 |
| quality | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31072076150/job/92527819849 |

CI retrieval:

```bash
gh run view 31072076150 --json jobs
gh run view 31072076150 --log
gh run download 31072076150 --name quality-reports
gh run download 31072076150 --name database-reports
gh run download 31072076150 --name clean-start-evidence
```

## Test evidence

Test result locations are the `quality-reports` and `database-reports` artifacts
from run `31072076150`. Reproduce the repository checks with:

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

- https://github.com/wyx-no1/product-pdf-qr/pull/29#discussion_r3720879396
- https://github.com/wyx-no1/product-pdf-qr/pull/29#discussion_r3724757312
- https://github.com/wyx-no1/product-pdf-qr/pull/29#discussion_r3725049220
- https://github.com/wyx-no1/product-pdf-qr/pull/29#discussion_r3725095608
- https://github.com/wyx-no1/product-pdf-qr/pull/29#discussion_r3725110453
- https://github.com/wyx-no1/product-pdf-qr/pull/29#discussion_r3725136517

Review record URLs:

- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4864759837
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4869611325
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4869986784
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4870039705
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4870056233
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4870084841
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4870094997
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4870532024
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4870533099
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4870533234
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4870533343
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4870684001
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4871073239
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4871104702
- https://github.com/wyx-no1/product-pdf-qr/pull/29#pullrequestreview-4871245925

Independent retrieval:

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/29/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/29/reviews
```
