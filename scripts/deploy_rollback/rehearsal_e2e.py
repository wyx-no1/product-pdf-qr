"""Drive real PR2B wrappers and PR2A restore against isolated synthetic state."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from alembic import command
from alembic.config import Config

from scripts.deploy_rollback.model import (
    REQUIRED_COMPATIBILITY_ACTIONS,
    REQUIRED_RECOVERY_CONFIG,
    ReleaseStore,
    atomic_write,
    canonical_json,
    digest_json,
    format_time,
    release_identity,
    validate_watermark,
)
from scripts.deploy_rollback.rehearsal_fixture import _reset_b0
from scripts.deploy_rollback.watermark import build_watermark

ROOT = Path(__file__).parents[2]
BACKUP_ID = f"20260812T033000Z-{'b' * 32}"


def _fence_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "scripts.deploy_rollback.rehearsal_fixture",
        "publication-fence",
    ]


def _run(
    arguments: list[str], *, cwd: Path, environment: dict[str, str], expected: int = 0
) -> None:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != expected:
        raise RuntimeError(
            f"synthetic command failed ({result.returncode}, expected {expected}): "
            f"{' '.join(arguments)}\nstdout={result.stdout}\nstderr={result.stderr}"
        )


def _artifact(content: bytes, retained_until: datetime) -> dict[str, Any]:
    return {
        "content_b64": base64.b64encode(content).decode(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "retained_until": format_time(retained_until),
    }


def _artifact_set(
    checkout: Path,
    marker: str,
    *,
    commit: str,
    retained_until: datetime,
) -> tuple[dict[str, Any], dict[str, str]]:
    digests = {
        component: hashlib.sha256(f"{marker}:{component}".encode()).hexdigest()
        for component in ("app", "migrate", "proxy", "db", "certbot", "pr2a")
    }
    digests["migrate"] = digests["app"]
    images = {
        component: f"synthetic.invalid/{component}:{marker}@sha256:{digest}"
        for component, digest in digests.items()
    }
    images["migrate"] = images["app"]
    image_ids = {
        reference: hashlib.sha256(f"id:{marker}:{component}".encode()).hexdigest()
        for component, reference in images.items()
    }
    recovery = {
        name: _artifact((checkout / name).read_bytes(), retained_until)
        for name in sorted(REQUIRED_RECOVERY_CONFIG)
    }
    runtime = canonical_json(
        {
            "APP_PORT": "8000",
            "STORAGE_ROOT": "/data/files",
            "PUBLIC_DOMAIN": "synthetic.invalid",
        }
    )
    return (
        {
            "commit": commit,
            "alembic_revision": "20260801_0001" if marker == "stable" else "20260804_0002",
            "migration_sha": "1" * 40 if marker == "stable" else "2" * 40,
            "images": images,
            "image_evidence": {
                component: {
                    "registry_digest": digest,
                    "image_id_digest": image_ids[images[component]],
                    "prefetched": True,
                    "retained_until": format_time(retained_until),
                }
                for component, digest in digests.items()
            },
            "recovery_config": recovery,
            "app_config": {"app-runtime.json": _artifact(runtime, retained_until)},
            "secret_references": {
                "database": f"vault://synthetic/database/{marker}/v1",
                "session": f"vault://synthetic/session/{marker}/v1",
                "acme": f"vault://synthetic/acme/{marker}/v1",
            },
        },
        image_ids,
    )


def _record(
    checkout: Path,
    *,
    release_id: str,
    compatible: bool,
    w0: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    now = datetime.now(tz=UTC)
    retained_until = now + timedelta(days=30)
    commit_sha = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    stable, stable_ids = _artifact_set(
        checkout, "stable", commit=commit_sha, retained_until=retained_until
    )
    candidate, candidate_ids = _artifact_set(
        checkout, "candidate", commit="3" * 40, retained_until=retained_until
    )
    approval = {
        "approval_id": f"approval-{release_id}",
        "approver": "synthetic-owner",
        "approved_at": format_time(now - timedelta(minutes=2)),
    }
    record: dict[str, Any] = {
        "schema_version": 1,
        "release_id": release_id,
        "environment_id": "synthetic-pr2b-e2e",
        "declared_at": format_time(now),
        "rollback_window_ends_at": format_time(retained_until),
        "stable": stable,
        "candidate": candidate,
        "pre_release_backup": {
            "backup_id": BACKUP_ID,
            "source_commit": stable["commit"],
            "images": stable["images"],
            "config_sha256": {
                name: artifact["sha256"]
                for name, artifact in sorted(stable["recovery_config"].items())
            },
            "alembic_revision": stable["alembic_revision"],
            "frozen_at": format_time(now - timedelta(minutes=10)),
            "completed_at": format_time(now - timedelta(minutes=9)),
            "encrypted": True,
            "manifest_authenticated": True,
            "completion_last": True,
            "remote_verified": True,
            "preflight_retrievable": True,
            "g19_watermark_sha256": validate_watermark(w0),
        },
        "compatibility": {
            "verdict": "compatible" if compatible else "incompatible",
            "migration_owner": "synthetic-database-owner",
            "decided_at": format_time(now - timedelta(minutes=3)),
            "release_identity": "",
            "approval": deepcopy(approval),
            "full_read_write_actions": {
                action: compatible for action in sorted(REQUIRED_COMPATIBILITY_ACTIONS)
            },
            "g19_rehearsal": {
                "passed": True,
                "run_id": f"g19-{release_id}",
                "release_identity": "",
            },
        },
        "release_approval": deepcopy(approval),
        "publication_fence": {
            "command_sha256": digest_json(_fence_command()),
            "readiness_bypass_only": True,
            "approval": deepcopy(approval),
        },
        "stable_isolation_smoke": {
            "passed": True,
            "run_id": f"smoke-{release_id}",
            "release_identity": "",
        },
        "pre_publication_plan": {"roll_forward": None, "lossy_recovery": None},
    }
    identity = release_identity(record)
    record["compatibility"]["release_identity"] = identity
    record["compatibility"]["g19_rehearsal"]["release_identity"] = identity
    record["stable_isolation_smoke"]["release_identity"] = identity
    if not compatible:
        record["pre_publication_plan"]["lossy_recovery"] = {
            "preapproved": True,
            "authorization_record": f"loss-{release_id}",
            "loss_start": format_time(now - timedelta(hours=1)),
            "loss_end": format_time(now + timedelta(hours=1)),
            "release_identity": identity,
            "approval": deepcopy(approval),
        }
    return record, stable_ids | candidate_ids


def _prepare_checkout(run_root: Path) -> Path:
    checkout = run_root / "checkout"
    shutil.copytree(
        ROOT,
        checkout,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "reports", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"
        ),
    )
    _run(["git", "init", "-q"], cwd=checkout, environment=dict(os.environ))
    _run(
        ["git", "config", "user.email", "synthetic@example.invalid"],
        cwd=checkout,
        environment=dict(os.environ),
    )
    _run(
        ["git", "config", "user.name", "Synthetic G19"],
        cwd=checkout,
        environment=dict(os.environ),
    )
    _run(["git", "add", "."], cwd=checkout, environment=dict(os.environ))
    _run(
        ["git", "commit", "-q", "-m", "synthetic rehearsal checkout"],
        cwd=checkout,
        environment=dict(os.environ),
    )
    return checkout


def _write_private(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(0o600)


def _add_w1(migration_url: str, file_root: Path) -> None:
    with psycopg.connect(
        migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    ) as connection:
        connection.execute(
            """
            INSERT INTO products (
                id, code, public_token, status, current_version_id,
                created_at, updated_at, name
            ) VALUES (
                2, 'W1_PRODUCT', '1123456789ABCDEFGHJKMNPQRS', 'active', NULL,
                '2026-08-12T00:01:00Z', '2026-08-12T00:01:00Z', 'W1'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO audit_events (
                id, occurred_at, actor_type, actor_id, action, target_type,
                target_id, product_code, result, request_id, detail
            ) VALUES (
                2, '2026-08-12T00:01:00Z', 'admin', 1, 'w1_public_write',
                'product', 2, 'W1_PRODUCT', 'success', NULL,
                '{"must_survive": true}'::jsonb
            )
            """
        )
    (file_root / "w1-after-cutover.pdf").write_bytes(b"%PDF-1.4\nsynthetic W1\n")


def _service_state(path: Path, record: dict[str, Any], image_ids: dict[str, str]) -> None:
    atomic_write(
        path,
        canonical_json(
            {
                "services": {
                    "app": {
                        "running": True,
                        "image": record["candidate"]["images"]["app"],
                    },
                    "proxy": {"running": False},
                    "db": {"running": True},
                    "certbot": {"running": True},
                },
                "image_ids": image_ids,
                "calls": [],
                "fence": {
                    "engaged": False,
                    "fence_id": "",
                    "release_id": "",
                    "operation_id": "",
                    "environment_id": "",
                },
            }
        ),
    )


def _common_environment(
    checkout: Path,
    run_root: Path,
    record: dict[str, Any],
    service_state: Path,
    migration_url: str,
    backup_url: str,
    file_root: Path,
) -> dict[str, str]:
    fake_bin = run_root / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    python = sys.executable
    for name, fixture_command in (("docker", "docker"), ("curl", "curl")):
        wrapper = fake_bin / name
        wrapper.write_text(
            f"#!/bin/sh\nexec '{python}' -m scripts.deploy_rollback.rehearsal_fixture "
            f'{fixture_command} "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    marker = run_root / "environment.json"
    atomic_write(
        marker,
        canonical_json(
            {
                "schema_version": 1,
                "kind": "synthetic",
                "environment_id": record["environment_id"],
                "docker_context": "synthetic-pr2b-e2e",
                "compose_project": "synthetic-pr2b-e2e",
                "resource_prefix": "synthetic-pr2b-e2e",
                "target_marker": "SYNTHETIC_PR2B_LOCAL_ONLY",
                "publication_fence_command": _fence_command(),
            }
        ),
    )
    external_body = "synthetic stable readiness\n"
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "PYTHONPATH": str(checkout),
            "PR2A_LOCK_DIRECTORY": str(run_root / "shared-lock"),
            "PR2B_PYTHON": python,
            "PR2B_ENVIRONMENT_MARKER": str(marker),
            "PR2B_ENVIRONMENT_CONFIRMATION": "",
            "PR2B_STABLE_CHECKOUT": str(checkout),
            "PR2B_SYNTHETIC_SERVICE_STATE": str(service_state),
            "PR2B_SYNTHETIC_PRODUCTION_ENV": str(checkout / ".env.prod"),
            "PR2B_SYNTHETIC_MIGRATION_URL": migration_url,
            "PR2B_DATABASE_URL": backup_url,
            "PR2B_FILE_ROOT": str(file_root),
            "PR2B_SYNTHETIC_EXTERNAL_BODY": external_body,
            "PR2B_SYNTHETIC_CANDIDATE_APP_IMAGE": record["candidate"]["images"]["app"],
            "PR2B_SYNTHETIC_EXPECTED_APP_IMAGE": record["stable"]["images"]["app"],
            "PR2B_WATERMARK_COMMAND_JSON": json.dumps(
                [python, "-m", "scripts.deploy_rollback.watermark"]
            ),
            "PR2B_NONMUTATING_VALIDATION_COMMAND_JSON": json.dumps(
                [python, "-m", "scripts.deploy_rollback.rehearsal_fixture", "assert-w1"]
            ),
            "PR2B_CANDIDATE_VALIDATION_COMMAND_JSON": json.dumps(
                [
                    python,
                    "-m",
                    "scripts.deploy_rollback.rehearsal_fixture",
                    "candidate-validation",
                ]
            ),
            "PR2B_EXTERNAL_READINESS_COMMAND_JSON": json.dumps(
                [python, "-m", "scripts.deploy_rollback.rehearsal_fixture", "assert-ready"]
            ),
            "PR2B_MIGRATION_COMMAND_JSON": json.dumps(
                [python, "-m", "scripts.deploy_rollback.rehearsal_fixture", "noop"]
            ),
            "PR2B_ISOLATED_VALIDATION_COMMAND_JSON": json.dumps(
                [
                    python,
                    "-m",
                    "scripts.deploy_rollback.rehearsal_fixture",
                    "isolated-full-validation",
                ]
            ),
            "PR2B_PUBLICATION_COMMAND_JSON": json.dumps(
                [python, "-m", "scripts.deploy_rollback.rehearsal_fixture", "publish-candidate"]
            ),
            "PR2B_PROXY_AUTHORIZATION_PATH": str(run_root / "proxy-authorization.json"),
        }
    )
    expected_digest = hashlib.sha256(external_body.encode()).hexdigest()
    prod_lines = [
        f"APP_IMAGE={record['stable']['images']['app']}",
        f"DB_IMAGE={record['stable']['images']['db']}",
        f"PROXY_IMAGE={record['stable']['images']['proxy']}",
        f"CERTBOT_IMAGE={record['stable']['images']['certbot']}",
        "PUBLIC_DOMAIN=synthetic.invalid",
    ]
    backup_lines = [
        f"BACKUP_IMAGE={record['stable']['images']['pr2a']}",
        "RESTORE_EXTERNAL_READINESS_URL=https://synthetic.invalid/ready",
        f"RESTORE_EXTERNAL_EXPECTED_SHA256={expected_digest}",
    ]
    _write_private(checkout / ".env.prod", ("\n".join(prod_lines) + "\n").encode())
    _write_private(checkout / ".env.backup", ("\n".join(backup_lines) + "\n").encode())
    return environment


def _publication(
    checkout: Path,
    environment: dict[str, str],
    *,
    release_id: str,
    operation_id: str,
    public: bool,
) -> None:
    wrapper = str(checkout / "scripts/deploy_rollback/publication-run.sh")
    for stage in ("prepare", "migrated", "isolated_validated"):
        _run(
            [wrapper, stage, release_id, operation_id, "synthetic-operator"],
            cwd=checkout,
            environment=environment,
        )
    if public:
        _run(
            [wrapper, "public_cutover", release_id, operation_id, "synthetic-operator"],
            cwd=checkout,
            environment=environment,
        )


def _scenario_paths(run_root: Path, prefix: str) -> dict[str, Path]:
    root = run_root / prefix
    root.mkdir()
    return {
        "root": root,
        "store": root / "releases",
        "state": root / "publication.json",
        "watermark": root / "watermark.json",
        "rto": root / "rto.json",
        "audit": root / "audit.jsonl",
        "runtime": root / "runtime.json",
        "result": root / "result.json",
        "service": root / "services.json",
        "fence": root / "publication-fence.json",
    }


def _bind_paths(environment: dict[str, str], paths: dict[str, Path]) -> None:
    environment.update(
        {
            "PR2B_RELEASE_STORE": str(paths["store"]),
            "PR2B_PUBLICATION_STATE": str(paths["state"]),
            "PR2B_WATERMARK_FILE": str(paths["watermark"]),
            "PR2B_RTO_STATE": str(paths["rto"]),
            "PR2B_AUDIT_LOG": str(paths["audit"]),
            "PR2B_RUNTIME_IDENTITY": str(paths["runtime"]),
            "PR2B_RESULT": str(paths["result"]),
            "PR2B_SYNTHETIC_SERVICE_STATE": str(paths["service"]),
            "PR2B_PUBLICATION_FENCE_STATE": str(paths["fence"]),
        }
    )


def run_round(run_root: Path, migration_url: str, backup_url: str) -> None:
    checkout = _prepare_checkout(run_root)
    configuration = Config(str(checkout / "alembic.ini"))
    os.environ["DATABASE_URL"] = migration_url
    try:
        command.downgrade(configuration, "base")
    except Exception:
        pass
    command.upgrade(configuration, "head")
    file_root = run_root / "files"
    file_root.mkdir()
    fixture_environment = {
        "PR2B_SYNTHETIC_MIGRATION_URL": migration_url,
        "PR2B_FILE_ROOT": str(file_root),
    }
    os.environ.update(fixture_environment)
    _reset_b0()
    w0 = build_watermark(database_url=backup_url, file_root=file_root)

    # Compatible path: release rehearsal is mutating; production switch is read-only.
    compatible_paths = _scenario_paths(run_root, "compatible")
    compatible, image_ids = _record(
        checkout, release_id="release-compatible", compatible=True, w0=w0
    )
    ReleaseStore(compatible_paths["store"]).seal(compatible)
    atomic_write(compatible_paths["watermark"], canonical_json(w0))
    _service_state(compatible_paths["service"], compatible, image_ids)
    environment = _common_environment(
        checkout,
        run_root,
        compatible,
        compatible_paths["service"],
        migration_url,
        backup_url,
        file_root,
    )
    _bind_paths(environment, compatible_paths)
    environment["PR2B_ENVIRONMENT_CONFIRMATION"] = (
        "synthetic:synthetic-pr2b-e2e:publication-compatible"
    )
    _publication(
        checkout,
        environment,
        release_id=compatible["release_id"],
        operation_id="publication-compatible",
        public=True,
    )
    _add_w1(migration_url, file_root)
    w1 = build_watermark(database_url=backup_url, file_root=file_root)
    atomic_write(compatible_paths["watermark"], canonical_json(w1))
    environment["PR2B_ENVIRONMENT_CONFIRMATION"] = (
        "synthetic:synthetic-pr2b-e2e:rollback-compatible"
    )
    environment["PR2B_PROXY_CONTINUOUSLY_ISOLATED"] = "no"
    _run(
        [
            str(checkout / "scripts/deploy_rollback/rollback-run.sh"),
            compatible["release_id"],
            "rollback-compatible",
            "synthetic-operator",
        ],
        cwd=checkout,
        environment=environment,
    )
    if build_watermark(database_url=backup_url, file_root=file_root) != w1:
        raise RuntimeError("compatible wrapper did not mechanically preserve exact W1")
    service = json.loads(compatible_paths["service"].read_text(encoding="utf-8"))
    if (
        service["services"]["app"]["image"] != compatible["stable"]["images"]["app"]
        or service["services"]["proxy"]["running"] is not True
        or any("restore-database" in call for call in service["calls"])
    ):
        raise RuntimeError("compatible wrapper identity/data semantics are incorrect")

    # Pre-public path: the real PR2B wrapper must hand off to the real PR2A entrypoint.
    _reset_b0()
    path_one_paths = _scenario_paths(run_root, "path-one")
    path_one, image_ids = _record(checkout, release_id="release-path-one", compatible=True, w0=w0)
    ReleaseStore(path_one_paths["store"]).seal(path_one)
    atomic_write(path_one_paths["watermark"], canonical_json(w0))
    _service_state(path_one_paths["service"], path_one, image_ids)
    environment = _common_environment(
        checkout,
        run_root,
        path_one,
        path_one_paths["service"],
        migration_url,
        backup_url,
        file_root,
    )
    _bind_paths(environment, path_one_paths)
    environment["PR2B_ENVIRONMENT_CONFIRMATION"] = (
        "synthetic:synthetic-pr2b-e2e:publication-path-one"
    )
    _publication(
        checkout,
        environment,
        release_id=path_one["release_id"],
        operation_id="publication-path-one",
        public=False,
    )
    environment["PR2B_ENVIRONMENT_CONFIRMATION"] = "synthetic:synthetic-pr2b-e2e:rollback-path-one"
    environment["PR2B_PROXY_CONTINUOUSLY_ISOLATED"] = "yes"
    _run(
        [
            str(checkout / "scripts/deploy_rollback/rollback-run.sh"),
            path_one["release_id"],
            "rollback-path-one",
            "synthetic-operator",
        ],
        cwd=checkout,
        environment=environment,
    )
    if build_watermark(database_url=backup_url, file_root=file_root) != w0:
        raise RuntimeError("path one did not freshly verify restored B0")
    service = json.loads(path_one_paths["service"].read_text(encoding="utf-8"))
    if (
        service["services"]["app"]["image"] != path_one["stable"]["images"]["app"]
        or service["services"]["proxy"]["running"] is not True
        or service["fence"]["engaged"] is not False
        or not any("restore-database" in call for call in service["calls"])
        or not any("restore-files" in call for call in service["calls"])
        or service["calls"].index("publication-fence:engage")
        > next(index for index, call in enumerate(service["calls"]) if "restore-database" in call)
        or service["calls"].index("publication-fence:publish")
        < next(index for index, call in enumerate(service["calls"]) if "restore-files" in call)
    ):
        raise RuntimeError("path one did not fence the exact stable PR2A restore")

    # Incompatible authorized path: decision 78 first, then exact stable identity and B0.
    _reset_b0()
    lossy_paths = _scenario_paths(run_root, "lossy")
    lossy, image_ids = _record(checkout, release_id="release-lossy", compatible=False, w0=w0)
    ReleaseStore(lossy_paths["store"]).seal(lossy)
    atomic_write(lossy_paths["watermark"], canonical_json(w0))
    _service_state(lossy_paths["service"], lossy, image_ids)
    environment = _common_environment(
        checkout,
        run_root,
        lossy,
        lossy_paths["service"],
        migration_url,
        backup_url,
        file_root,
    )
    _bind_paths(environment, lossy_paths)
    environment["PR2B_ENVIRONMENT_CONFIRMATION"] = "synthetic:synthetic-pr2b-e2e:publication-lossy"
    _publication(
        checkout,
        environment,
        release_id=lossy["release_id"],
        operation_id="publication-lossy",
        public=True,
    )
    _add_w1(migration_url, file_root)
    lossy_w1 = build_watermark(database_url=backup_url, file_root=file_root)
    atomic_write(lossy_paths["watermark"], canonical_json(lossy_w1))
    environment["PR2B_ENVIRONMENT_CONFIRMATION"] = "synthetic:synthetic-pr2b-e2e:rollback-lossy"
    environment["PR2B_PROXY_CONTINUOUSLY_ISOLATED"] = "no"
    _run(
        [
            str(checkout / "scripts/deploy_rollback/rollback-run.sh"),
            lossy["release_id"],
            "rollback-lossy",
            "synthetic-operator",
        ],
        cwd=checkout,
        environment=environment,
        expected=78,
    )
    now = datetime.now(tz=UTC)
    challenge = "synthetic-one-time-lossy-challenge"
    onsite = hashlib.sha256(b"synthetic-encrypted-W1-retention").hexdigest()
    approval = deepcopy(lossy["pre_publication_plan"]["lossy_recovery"]["approval"])
    authorization = {
        "schema_version": 1,
        "release_id": lossy["release_id"],
        "release_identity": release_identity(lossy),
        "operation_id": "rollback-lossy",
        "environment_id": lossy["environment_id"],
        "backup_id": BACKUP_ID,
        "operator": "synthetic-operator",
        "authorization_record": f"loss-{lossy['release_id']}",
        "approved_at": format_time(now - timedelta(minutes=1)),
        "expires_at": format_time(now + timedelta(minutes=20)),
        "one_time_challenge_sha256": hashlib.sha256(challenge.encode()).hexdigest(),
        "loss_start": format_time(now - timedelta(minutes=30)),
        "loss_end": format_time(now + timedelta(minutes=10)),
        "onsite_retention_sha256": onsite,
        "reconciliation_plan": "compare retained W1 relation/file/audit manifest",
        "approval": approval,
    }
    authorization_path = lossy_paths["root"] / "authorization.json"
    challenge_path = lossy_paths["root"] / "challenge"
    _write_private(authorization_path, canonical_json(authorization))
    _write_private(challenge_path, (challenge + "\n").encode())
    used = lossy_paths["root"] / "used-challenges"
    used.mkdir(mode=0o700)
    environment.update(
        {
            "PR2B_ENVIRONMENT_ID": lossy["environment_id"],
            "PR2B_USED_CHALLENGES": str(used),
        }
    )
    _run(
        [
            str(checkout / "scripts/deploy_rollback/authorized-lossy-run.sh"),
            lossy["release_id"],
            "rollback-lossy",
            "synthetic-operator",
            str(authorization_path),
            str(challenge_path),
            onsite,
        ],
        cwd=checkout,
        environment=environment,
    )
    if build_watermark(database_url=backup_url, file_root=file_root) != w0:
        raise RuntimeError("authorized path did not freshly verify restored B0")
    service = json.loads(lossy_paths["service"].read_text(encoding="utf-8"))
    if (
        service["services"]["app"]["image"] != lossy["stable"]["images"]["app"]
        or service["services"]["proxy"]["running"] is not True
        or service["fence"]["engaged"] is not False
        or service["calls"].index("publication-fence:engage")
        > next(index for index, call in enumerate(service["calls"]) if "restore-database" in call)
        or service["calls"].index("publication-fence:publish")
        < next(index for index, call in enumerate(service["calls"]) if "restore-files" in call)
    ):
        raise RuntimeError("authorized path exposed candidate/B0 outside the fence")
    print("real_wrappers=publication,rollback,authorized-lossy,pr2a-restore")
    print("real_postgresql=true real_file_state=true")
    print("path_one_b0_verified=true path_two_w1_preserved=true authorized_b0_verified=true")
    print("publication_fence_continuous=true production_validation_nonmutating=true")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: rehearsal_e2e.py RUN_ROOT")
    migration_url = os.environ.get("TEST_MIGRATION_DATABASE_URL", "")
    backup_url = os.environ.get("TEST_BACKUP_DATABASE_URL", "")
    if not migration_url or not backup_url:
        raise SystemExit("isolated TEST_MIGRATION_DATABASE_URL/TEST_BACKUP_DATABASE_URL required")
    run_round(Path(sys.argv[1]).resolve(), migration_url, backup_url)


if __name__ == "__main__":
    main()
