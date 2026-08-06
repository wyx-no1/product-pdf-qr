# Evidence Snapshot for PR #33

## Binding

| Field | Value |
|---|---|
| PR number | `#33` |
| PR title | docs: 收编批量下载二维码默认规则 |
| PR URL | https://github.com/wyx-no1/product-pdf-qr/pull/33 |
| Source branch | `docs/req-v2-batch-default` |
| Target branch | `main` |
| Code commit SHA | `94f825c39aebd2a26b457947b6c31a7812148f3d` |
| Merge base | `bd99824380af984162604fdcdffebe55f3456a35` |
| CI run ID | `31083590078` |
| CI conclusion | `success` |
| CI URL | https://github.com/wyx-no1/product-pdf-qr/actions/runs/31083590078 |
| CI definition status | `trusted` |
| Trusted CI definition hash | `baf58ec1dce6ae98589be8220dbb968f8f1c4cdbb65520346b96ebd4dbfbfe91` |
| Candidate CI definition hash | `baf58ec1dce6ae98589be8220dbb968f8f1c4cdbb65520346b96ebd4dbfbfe91` |
| Created at | `2026-08-06T08:09:43.252115+00:00` |

> The CI result above belongs to code commit `94f825c39aebd2a26b457947b6c31a7812148f3d`, not to the later
> Evidence commit. The Evidence commit must not be used as a new generation trigger;
> doing so would create a CI/Evidence loop.



## Change overview

| Metric | Value |
|---|---:|
| Changed files | 1 |
| Added lines | 2 |
| Deleted lines | 0 |

The authoritative patch was generated with the three-dot comparison
`git diff origin/main...94f825c39aebd2a26b457947b6c31a7812148f3d -- . ':(exclude)docs/evidence/**'`.
The entire `docs/evidence/**` tree is excluded to prevent self-reference. No source
files are copied into this directory.

## Code commits

| Commit | Subject |
|---|---|
| `94f825c39aebd2a26b457947b6c31a7812148f3d` | chore: retrigger PR33 evidence snapshot |
| `0df5e4cf67b316f97a79a7d79795c41933526d66` | Revert "docs: add PR33 review evidence" |
| `c2303e5adc9c840b57b08198a2b99cccc732c237` | docs: add PR33 review evidence |
| `30f3f2fa01f84a1ecdb8899c7045a7b3f0382dd4` | chore: merge main into docs requirements branch |
| `6b813fb7953d41e182edf3e827aa14666ceecfc3` | docs: define batch QR download default |

## Position and known limitations

This directory is a factual index and snapshot for PR #33. It records a
fixed field set chosen by the tool's designers, so automatic generation does not
eliminate selection bias or blind spots. `diff.patch` is the complete authoritative
change record for scope questions, excluding only `docs/evidence/**`.

Evidence does not replace source review. An Advisor must read both this snapshot and
the source at code commit `94f825c39aebd2a26b457947b6c31a7812148f3d`. If Evidence and source disagree, source is
authoritative and the Evidence error must be reported. The tool cannot mechanically
prove that an Advisor actually read the source.
