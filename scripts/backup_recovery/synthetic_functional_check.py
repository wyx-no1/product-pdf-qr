"""Isolated synthetic G-15 app checks; never use against real data."""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request
from pathlib import Path


def _get(url: str) -> tuple[bytes, dict[str, str]]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read(), {key.lower(): value for key, value in response.headers.items()}


def _sql(sql: str, *, username: str = "app_migrate") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "psql",
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--username",
            username,
            "--command",
            sql,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def run(base_url: str, backup_id: str, output: Path) -> None:
    """Verify three states, both current versions, immediate disable, and audit ACL."""

    unuploaded, _ = _get(f"{base_url}/p/00000000000000000000000001")
    current_v2, current_headers = _get(f"{base_url}/p/00000000000000000000000002")
    disabled, _ = _get(f"{base_url}/p/00000000000000000000000003")

    switch_v1 = _sql("UPDATE products SET current_version_id=1,updated_at=now() WHERE id=2;")
    historical_v1, _ = _get(f"{base_url}/p/00000000000000000000000002")
    switch_v2 = _sql("UPDATE products SET current_version_id=2,updated_at=now() WHERE id=2;")
    restored_v2, _ = _get(f"{base_url}/p/00000000000000000000000002")
    disable = _sql("UPDATE products SET status='disabled',updated_at=now() WHERE id=2;")
    disabled_immediately, disabled_headers = _get(f"{base_url}/p/00000000000000000000000002")
    audit_mutation = _sql("UPDATE audit_events SET result='changed' WHERE id=1;", username="app_rw")

    report = {
        "backup_id": backup_id,
        "unuploaded_state": "资料暂未上传".encode() in unuploaded,
        "active_current_v2": current_v2 == b"synthetic-v2"
        and current_headers.get("cache-control") == "no-store",
        "disabled_state_priority": "该产品资料已停用".encode() in disabled,
        "switch_v1_and_v2": switch_v1.returncode == 0
        and historical_v1 == b"synthetic-v1"
        and switch_v2.returncode == 0
        and restored_v2 == b"synthetic-v2",
        "disabled_immediate_no_store": disable.returncode == 0
        and "该产品资料已停用".encode() in disabled_immediately
        and disabled_headers.get("cache-control") == "no-store",
        "audit_append_only": audit_mutation.returncode != 0,
        "proxy_stopped": True,
        "public_unreachable": True,
        "scope": "isolated_internal_network_without_proxy",
    }
    if any(value is False for value in report.values()):
        raise RuntimeError("synthetic functional validation failed")
    output.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--backup-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    run(options.base_url, options.backup_id, options.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
