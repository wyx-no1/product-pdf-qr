from __future__ import annotations

from pathlib import Path


def test_evidence_job_blocks_after_three_code_jobs_with_minimal_write_permission() -> None:
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    evidence = workflow.split("\n  evidence:\n", maxsplit=1)[1]

    assert "permissions:\n  contents: read\n" in workflow
    assert workflow.count("contents: write") == 1
    assert (
        "permissions:\n      actions: read\n      contents: write\n      pull-requests: read"
        in evidence
    )
    assert "needs:\n      - quality\n      - database\n      - container" in evidence
    assert evidence.count("python -m scripts.ao evidence") == 1
    assert '--ci-run-id "$RUN_ID"' in evidence
    assert "GITHUB_TOKEN pushes do not start another workflow" in evidence
    assert "continue-on-error" not in evidence
