# Evidence Snapshot for PR #23

## Binding

| Field | Value |
|---|---|
| PR number | `#23` |
| PR title | docs: 收编产品名称与 Excel 导入名称规则 |
| PR URL | https://github.com/wyx-no1/product-pdf-qr/pull/23 |
| Source branch | `docs/req-v2-product-name` |
| Target branch | `main` |
| Code commit SHA | `363b46c2045d2eff4997c9130e7bcc9d5de1d209` |
| Merge base | `87e6d4afbb5560b490c845bd8630a3cf79cd2a5e` |
| CI run ID | `31001869414` |
| CI conclusion | `success` |
| CI URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31001869414 |
| CI definition status | `trusted` |
| Trusted CI definition hash | `8c030b5053fbef4bfed41d11670bee009689bd0f564830954e4a72ffd243a7cc` |
| Candidate CI definition hash | `8c030b5053fbef4bfed41d11670bee009689bd0f564830954e4a72ffd243a7cc` |
| Created at | `2026-08-05T11:50:01.760054+00:00` |

> The CI result above belongs to code commit `363b46c2045d2eff4997c9130e7bcc9d5de1d209`, not to the later
> Evidence commit. The Evidence commit must not be used as a new generation trigger;
> doing so would create a CI/Evidence loop.



## Change overview

| Metric | Value |
|---|---:|
| Changed files | 1 |
| Added lines | 9 |
| Deleted lines | 0 |

The authoritative patch was generated with the three-dot comparison
`git diff origin/main...363b46c2045d2eff4997c9130e7bcc9d5de1d209 -- . ':(exclude)docs/evidence/**'`.
The entire `docs/evidence/**` tree is excluded to prevent self-reference. No source
files are copied into this directory.

## Code commits

| Commit | Subject |
|---|---|
| `363b46c2045d2eff4997c9130e7bcc9d5de1d209` | docs: define product name requirements |

## Position and known limitations

This directory is a factual index and snapshot for PR #23. It records a
fixed field set chosen by the tool's designers, so automatic generation does not
eliminate selection bias or blind spots. `diff.patch` is the complete authoritative
change record for scope questions, excluding only `docs/evidence/**`.

Evidence does not replace source review. An Advisor must read both this snapshot and
the source at code commit `363b46c2045d2eff4997c9130e7bcc9d5de1d209`. If Evidence and source disagree, source is
authoritative and the Evidence error must be reported. The tool cannot mechanically
prove that an Advisor actually read the source.
