# Evidence Snapshot for PR #12

## Binding

| Field | Value |
|---|---|
| PR number | `#12` |
| PR title | feat: 自动生成 AO Evidence 并解析 Advisor workspace |
| PR URL | https://github.com/wyx-no1/product-pdf-qr/pull/12 |
| Source branch | `feat/ao-p0-1-evidence-snapshot` |
| Target branch | `main` |
| Code commit SHA | `80157fb3ae1e3a955eb0da29557dad80e57bc437` |
| Merge base | `af82f581c83bd023b4d17ccc4231a2802acf6f2c` |
| CI run ID | `30792140720` |
| CI conclusion | `success` |
| CI URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30792140720 |
| Created at | `2026-08-03T07:03:44.474517+00:00` |

> The CI result above belongs to code commit `80157fb3ae1e3a955eb0da29557dad80e57bc437`, not to the later
> Evidence commit. The Evidence commit must not be used as a new generation trigger;
> doing so would create a CI/Evidence loop.


## Change overview

| Metric | Value |
|---|---:|
| Changed files | 18 |
| Added lines | 3624 |
| Deleted lines | 0 |

The authoritative patch was generated with the three-dot comparison
`git diff origin/main...80157fb3ae1e3a955eb0da29557dad80e57bc437 -- . ':(exclude)docs/evidence/**'`.
The entire `docs/evidence/**` tree is excluded to prevent self-reference. No source
files are copied into this directory.

## Code commits

| Commit | Subject |
|---|---|
| `80157fb3ae1e3a955eb0da29557dad80e57bc437` | fix: secure AO evidence publication |
| `8182f02133bac7cc3fbeb77e503fa2553ca28b4f` | docs: add PR12 review evidence |
| `edc067f4836762f0729402e4fc545e708dfaeb43` | fix: close AO evidence review gaps |
| `705bfa105d26d1007e15294124eb5dd6dbfaad69` | docs: add PR12 review evidence |
| `744641de628ee664ab41e1a52308b7f747734263` | feat: automate AO evidence and advisor workspaces |

## Position and known limitations

This directory is a factual index and snapshot for PR #12. It records a
fixed field set chosen by the tool's designers, so automatic generation does not
eliminate selection bias or blind spots. `diff.patch` is the complete authoritative
change record for scope questions, excluding only `docs/evidence/**`.

Evidence does not replace source review. An Advisor must read both this snapshot and
the source at code commit `80157fb3ae1e3a955eb0da29557dad80e57bc437`. If Evidence and source disagree, source is
authoritative and the Evidence error must be reported. The tool cannot mechanically
prove that an Advisor actually read the source.
