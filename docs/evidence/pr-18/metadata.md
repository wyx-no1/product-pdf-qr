# Evidence Snapshot for PR #18

## Binding

| Field | Value |
|---|---|
| PR number | `#18` |
| PR title | feat: 增加 V1 产品管理、搜索与管理员身份能力 |
| PR URL | https://github.com/wyx-no1/product-pdf-qr/pull/18 |
| Source branch | `feat/admin-ui-minimal` |
| Target branch | `main` |
| Code commit SHA | `2556120bad9a955147df33a90f1c447e35c96bfe` |
| Merge base | `8e6fd10410a021b369be2c9b566467c5ef305f93` |
| CI run ID | `30906594651` |
| CI conclusion | `success` |
| CI URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30906594651 |
| CI definition status | `requires-re-review` |
| Trusted CI definition hash | `63976b239a441249877c960015bd943c4b60580a3f054c1960b0c5e71a012240` |
| Candidate CI definition hash | `3ad9d4300c6af359eff5bd6fbd0e5740f39e6b2f3595c3cd434689ef2eb2ce42` |
| Created at | `2026-08-05T02:26:07.517872+00:00` |

> The CI result above belongs to code commit `2556120bad9a955147df33a90f1c447e35c96bfe`, not to the later
> Evidence commit. The Evidence commit must not be used as a new generation trigger;
> doing so would create a CI/Evidence loop.


> **Re-review required:** repository-controlled CI gate inputs differ from the trusted
> default-branch definition. This Evidence must not receive or preserve a trusted
> green status.

## Change overview

| Metric | Value |
|---|---:|
| Changed files | 34 |
| Added lines | 4640 |
| Deleted lines | 125 |

The authoritative patch was generated with the three-dot comparison
`git diff origin/main...2556120bad9a955147df33a90f1c447e35c96bfe -- . ':(exclude)docs/evidence/**'`.
The entire `docs/evidence/**` tree is excluded to prevent self-reference. No source
files are copied into this directory.

## Code commits

| Commit | Subject |
|---|---|
| `2556120bad9a955147df33a90f1c447e35c96bfe` | test: stabilize concurrent login regression |
| `f3f9edaf49e8ee75cdd664137f18e909bddfb129` | fix: close remaining authentication review gaps |
| `3ef3e03652d80a8768dd159681567fdaa159924d` | fix: close admin security audit gaps |
| `81611b27f1c49ab44dce2a2442f681f87950a949` | feat: add product list search filters |
| `e4f659e9150cc9b305bdb8f8bc12ce7d717f36ac` | feat: add minimal admin authentication |
| `00f295d23d3307cd3de26aec24fd7f6b687ac7e3` | feat: persist admin product data |
| `ba17fb32764724c9755116ea194dc02d91b0f4c9` | feat: add minimal admin UI |

## Position and known limitations

This directory is a factual index and snapshot for PR #18. It records a
fixed field set chosen by the tool's designers, so automatic generation does not
eliminate selection bias or blind spots. `diff.patch` is the complete authoritative
change record for scope questions, excluding only `docs/evidence/**`.

Evidence does not replace source review. An Advisor must read both this snapshot and
the source at code commit `2556120bad9a955147df33a90f1c447e35c96bfe`. If Evidence and source disagree, source is
authoritative and the Evidence error must be reported. The tool cannot mechanically
prove that an Advisor actually read the source.
