# Evidence Snapshot for PR #12

## Binding

| Field | Value |
|---|---|
| PR number | `#12` |
| PR title | feat: 自动生成 AO Evidence 并解析 Advisor workspace |
| PR URL | https://github.com/wyx-no1/product-pdf-qr/pull/12 |
| Source branch | `feat/ao-p0-1-evidence-snapshot` |
| Target branch | `main` |
| Code commit SHA | `744641de628ee664ab41e1a52308b7f747734263` |
| Merge base | `af82f581c83bd023b4d17ccc4231a2802acf6f2c` |
| CI run ID | `30788671105` |
| CI conclusion | `success` |
| CI URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30788671105 |
| Created at | `2026-08-03T06:00:14.407162+00:00` |

> The CI result above belongs to code commit `744641de628ee664ab41e1a52308b7f747734263`, not to the later
> Evidence commit. The Evidence commit must not be used as a new generation trigger;
> doing so would create a CI/Evidence loop.

## Change overview

| Metric | Value |
|---|---:|
| Changed files | 15 |
| Added lines | 2688 |
| Deleted lines | 0 |

The authoritative patch was generated with the three-dot comparison
`git diff origin/main...744641de628ee664ab41e1a52308b7f747734263 -- . ':(exclude)docs/evidence/**'`.
The entire `docs/evidence/**` tree is excluded to prevent self-reference. No source
files are copied into this directory.

## Code commits

| Commit | Subject |
|---|---|
| `744641de628ee664ab41e1a52308b7f747734263` | feat: automate AO evidence and advisor workspaces |

## Position and known limitations

This directory is a factual index and snapshot for PR #12. It records a
fixed field set chosen by the tool's designers, so automatic generation does not
eliminate selection bias or blind spots. `diff.patch` is the complete authoritative
change record for scope questions, excluding only `docs/evidence/**`.

Evidence does not replace source review. An Advisor must read both this snapshot and
the source at code commit `744641de628ee664ab41e1a52308b7f747734263`. If Evidence and source disagree, source is
authoritative and the Evidence error must be reported. The tool cannot mechanically
prove that an Advisor actually read the source.
