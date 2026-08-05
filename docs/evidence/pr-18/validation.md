# Validation record for PR #18

Created at: `2026-08-05T02:26:07.517872+00:00`

This file records evidence locations and results, not an evaluation of their
correctness. When a record disagrees with its original source, the original source
is authoritative.

## CI status

| Field | Value |
|---|---|
| Run ID | `30906594651` |
| Code commit | `2556120bad9a955147df33a90f1c447e35c96bfe` |
| Status | `completed` |
| Conclusion | `success` |
| URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30906594651 |
| Merge base | `8e6fd10410a021b369be2c9b566467c5ef305f93` |

> This CI result applies to code commit `2556120bad9a955147df33a90f1c447e35c96bfe`, not to the Evidence commit
> that contains this directory. Do not regenerate Evidence to describe its own
> commit. A later code change requires a new successful CI run and a regenerated
> snapshot.


| Job | Conclusion | URL |
|---|---|---|
| container | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30906594651/job/91983003426 |
| database | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30906594651/job/91983003405 |
| quality | `success` | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30906594651/job/91983003425 |

CI retrieval:

```bash
gh run view 30906594651 --json jobs
gh run view 30906594651 --log
gh run download 30906594651 --name quality-reports
gh run download 30906594651 --name database-reports
gh run download 30906594651 --name clean-start-evidence
```

## Test evidence

Test result locations are the `quality-reports` and `database-reports` artifacts
from run `30906594651`. Reproduce the repository checks with:

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

- https://github.com/wyx-no1/product-pdf-qr/pull/18#discussion_r3711831452
- https://github.com/wyx-no1/product-pdf-qr/pull/18#discussion_r3711831460
- https://github.com/wyx-no1/product-pdf-qr/pull/18#discussion_r3711831463
- https://github.com/wyx-no1/product-pdf-qr/pull/18#discussion_r3711904100
- https://github.com/wyx-no1/product-pdf-qr/pull/18#discussion_r3711904667
- https://github.com/wyx-no1/product-pdf-qr/pull/18#discussion_r3711905142
- https://github.com/wyx-no1/product-pdf-qr/pull/18#discussion_r3711979302
- https://github.com/wyx-no1/product-pdf-qr/pull/18#discussion_r3711979310
- https://github.com/wyx-no1/product-pdf-qr/pull/18#discussion_r3711979318
- https://github.com/wyx-no1/product-pdf-qr/pull/18#discussion_r3712065359
- https://github.com/wyx-no1/product-pdf-qr/pull/18#discussion_r3712065789
- https://github.com/wyx-no1/product-pdf-qr/pull/18#discussion_r3712066210

Review record URLs:

- https://github.com/wyx-no1/product-pdf-qr/pull/18#pullrequestreview-4853620020
- https://github.com/wyx-no1/product-pdf-qr/pull/18#pullrequestreview-4853706561
- https://github.com/wyx-no1/product-pdf-qr/pull/18#pullrequestreview-4853707233
- https://github.com/wyx-no1/product-pdf-qr/pull/18#pullrequestreview-4853707789
- https://github.com/wyx-no1/product-pdf-qr/pull/18#pullrequestreview-4853794824
- https://github.com/wyx-no1/product-pdf-qr/pull/18#pullrequestreview-4853891616
- https://github.com/wyx-no1/product-pdf-qr/pull/18#pullrequestreview-4853892096
- https://github.com/wyx-no1/product-pdf-qr/pull/18#pullrequestreview-4853892527

Independent retrieval:

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/18/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/18/reviews
```
