# Advisor context for PR #18

## Required review order

1. Read `metadata.md` and verify the PR, branch, code commit, and CI binding.
2. Run the boundary command in `changed-files.md`.
3. Read `validation.md` and follow its original evidence links.
4. Inspect `diff.patch` for the complete three-dot change record.
5. Use the Advisor Workspace Resolver to inspect the source at code commit
   `2556120bad9a955147df33a90f1c447e35c96bfe` in detached HEAD state.

The original Evidence files are in `docs/evidence/pr-18/` on branch
`feat/admin-ui-minimal`. The source of record is the repository at code commit
`2556120bad9a955147df33a90f1c447e35c96bfe`.

## Non-substitution rule

This directory cannot replace source judgment. Evidence and the corresponding
source commit are both required. If they disagree, source is authoritative and the
Evidence error must be called out in the Advisor opinion. An opinion must record the
actual reviewed commit SHA so that the gate can invalidate it after a later code
change.
