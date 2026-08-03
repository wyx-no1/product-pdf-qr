from __future__ import annotations

from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"


def test_pr_ci_is_read_only_and_does_not_persist_checkout_credentials() -> None:
    workflow = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")

    assert "permissions:\n  contents: read\n" in workflow
    assert "contents: write" not in workflow
    assert "GH_TOKEN" not in workflow
    assert "python -m scripts.ao evidence" not in workflow
    assert workflow.count("uses: actions/checkout@v4") == 3
    assert workflow.count("persist-credentials: false") == 3


def test_trusted_publisher_never_executes_candidate_code_and_verifies_before_green() -> None:
    workflow = (WORKFLOWS / "evidence-publish.yml").read_text(encoding="utf-8")

    assert "workflow_run:" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "permissions:\n  contents: read\n" in workflow
    assert workflow.count("contents: write") == 1
    assert "statuses: write" in workflow
    assert "ref: ${{ github.event.repository.default_branch }}" in workflow
    assert "path: trusted" in workflow
    assert "path: candidate" in workflow
    assert workflow.count("persist-credentials: false") == 2
    assert "working-directory: trusted" in workflow
    assert "python -m scripts.ao evidence \\\n              --repo ../candidate" in workflow
    assert "python -m scripts.ao evidence-verify-head" in workflow
    verification = workflow.index("python -m scripts.ao evidence-verify-head")
    green_status = workflow.index('post_status "$evidence_sha" success')
    assert verification < green_status
    assert 'if [[ "$generation_result" == "skipped" ]]; then' in workflow
    assert "--require-prior-attestation" in workflow
    skip_branch = workflow.index('if [[ "$generation_result" == "skipped" ]]; then')
    skip_exit = workflow.index("exit 0", skip_branch)
    assert 'post_status "$evidence_sha" success' not in workflow[skip_branch:skip_exit]
    assert "github.head_ref" not in workflow
    assert "continue-on-error" not in workflow
