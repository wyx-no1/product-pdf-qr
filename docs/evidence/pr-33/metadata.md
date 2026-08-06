# Evidence Snapshot for PR #33

## Binding

| Field | Value |
|---|---|
| PR number | `#33` |
| PR title | docs: 收编批量下载二维码默认规则 |
| PR URL | https://github.com/wyx-no1/product-pdf-qr/pull/33 |
| Source branch | `docs/req-v2-batch-default` |
| Target branch | `main` |
| Code commit SHA | `30f3f2fa01f84a1ecdb8899c7045a7b3f0382dd4` |
| Merge base | `bd99824380af984162604fdcdffebe55f3456a35` |
| CI run ID | `31082995505` |
| CI conclusion | `success` |
| CI URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31082995505 |
| CI definition status | `trusted` |
| Trusted CI definition hash | `baf58ec1dce6ae98589be8220dbb968f8f1c4cdbb65520346b96ebd4dbfbfe91` |
| Candidate CI definition hash | `baf58ec1dce6ae98589be8220dbb968f8f1c4cdbb65520346b96ebd4dbfbfe91` |
| Created at | `2026-08-06T08:02:47.270537+00:00` |

> The CI result above belongs to code commit `30f3f2fa01f84a1ecdb8899c7045a7b3f0382dd4`, not to the later
> Evidence commit. The Evidence commit must not be used as a new generation trigger;
> doing so would create a CI/Evidence loop.



## Change overview

| Metric | Value |
|---|---:|
| Changed files | 1 |
| Added lines | 2 |
| Deleted lines | 0 |

The authoritative patch was generated with the three-dot comparison
`git diff origin/main...30f3f2fa01f84a1ecdb8899c7045a7b3f0382dd4 -- . ':(exclude)docs/evidence/**'`.
The entire `docs/evidence/**` tree is excluded to prevent self-reference. No source
files are copied into this directory.

## Code commits

| Commit | Subject |
|---|---|
| `30f3f2fa01f84a1ecdb8899c7045a7b3f0382dd4` | chore: merge main into docs requirements branch |
| `6b813fb7953d41e182edf3e827aa14666ceecfc3` | docs: define batch QR download default |

## Position and known limitations

This directory is a factual index and snapshot for PR #33. It records a
fixed field set chosen by the tool's designers, so automatic generation does not
eliminate selection bias or blind spots. `diff.patch` is the complete authoritative
change record for scope questions, excluding only `docs/evidence/**`.

Evidence does not replace source review. An Advisor must read both this snapshot and
the source at code commit `30f3f2fa01f84a1ecdb8899c7045a7b3f0382dd4`. If Evidence and source disagree, source is
authoritative and the Evidence error must be reported. The tool cannot mechanically
prove that an Advisor actually read the source.
