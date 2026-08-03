# Evidence Snapshot for PR #12

## Binding

| Field | Value |
|---|---|
| PR number | `#12` |
| PR title | feat: 自动生成 AO Evidence 并解析 Advisor workspace |
| PR URL | https://github.com/wyx-no1/product-pdf-qr/pull/12 |
| Source branch | `feat/ao-p0-1-evidence-snapshot` |
| Target branch | `main` |
| Code commit SHA | `811693afe7e6de4956f5925f830f5e3d75ee3e15` |
| Merge base | `af82f581c83bd023b4d17ccc4231a2802acf6f2c` |
| CI run ID | `30799823655` |
| CI conclusion | `success` |
| CI URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30799823655 |
| CI definition status | `requires-re-review` |
| Trusted CI definition hash | `9e55e4f64d866ad012491e981e9305336bbc73f817052d6152efed541e47a62f` |
| Candidate CI definition hash | `940367e05420714ab8dc20224ad1f98c05116e807098e27dc8fd653b2ad4f77e` |
| Created at | `2026-08-03T09:06:39.903010+00:00` |

> The CI result above belongs to code commit `811693afe7e6de4956f5925f830f5e3d75ee3e15`, not to the later
> Evidence commit. The Evidence commit must not be used as a new generation trigger;
> doing so would create a CI/Evidence loop.


> **Re-review required:** repository-controlled CI gate inputs differ from the trusted
> default-branch definition. This Evidence must not receive or preserve a trusted
> green status.

## Change overview

| Metric | Value |
|---|---:|
| Changed files | 21 |
| Added lines | 5003 |
| Deleted lines | 8 |

The authoritative patch was generated with the three-dot comparison
`git diff origin/main...811693afe7e6de4956f5925f830f5e3d75ee3e15 -- . ':(exclude)docs/evidence/**'`.
The entire `docs/evidence/**` tree is excluded to prevent self-reference. No source
files are copied into this directory.

## Code commits

| Commit | Subject |
|---|---|
| `811693afe7e6de4956f5925f830f5e3d75ee3e15` | fix: reject nested CI config overrides |
| `862d57d3948d715d127364ae2deb287ba12a7c0d` | fix: pin Ruff CI configuration |
| `f8f62b8af7ff4baa15387d1eecb1bf9ee9b0c084` | docs: add PR12 review evidence |
| `57180d81c1411c569797724e777b5d2ba912bb1e` | fix: bind evidence to trusted CI definition |
| `0d2546e383313afee770d365117bc1002a168eb9` | fix: trust source CI workflow definition |
| `6f3a25852d1935127c0818b672684b5daa850002` | docs: add PR12 review evidence |
| `279b970471cce613c6ad7eb3f8501936afebf036` | fix: verify trusted Evidence before skip |
| `b9f482464ebd73b6544a7ca6b333fd3c7a7c762c` | docs: add PR12 review evidence |
| `1873316ff372fc5ab7c4a1adb9131fc966b21cae` | fix: attest skipped Evidence snapshots |
| `3b745fb19fe2bcedba32e185c56d4287d1c82e06` | docs: add PR12 review evidence |
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
the source at code commit `811693afe7e6de4956f5925f830f5e3d75ee3e15`. If Evidence and source disagree, source is
authoritative and the Evidence error must be reported. The tool cannot mechanically
prove that an Advisor actually read the source.
