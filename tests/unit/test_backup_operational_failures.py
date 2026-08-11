"""Failure-path regressions for PR2A's production orchestration boundaries."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.backup_recovery import crypto
from scripts.backup_recovery import restore as restore_module
from scripts.backup_recovery.backup import BackupBuilder
from scripts.backup_recovery.model import (
    RestoreGuard,
    SafetyError,
    atomic_write,
    authenticate_manifest,
    authenticate_restore_verification,
    canonical_json,
    generation_tags,
    verify_restore_verification,
)
from scripts.backup_recovery.remote import LocalRemote, RcloneRemote
from scripts.backup_recovery.restore import RestoreEngine
from scripts.retention import rotate as rotate_module

ROOT = Path(__file__).parents[2]
MANIFEST_AUTHENTICATION_KEY = b"k" * 32
MANIFEST_VERIFICATION_KEY = (
    Ed25519PrivateKey.from_private_bytes(MANIFEST_AUTHENTICATION_KEY)
    .public_key()
    .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
)
MANIFEST_AUTHENTICATION_KEY_ID = "manifest-auth-test"
RESTORE_VERIFICATION_AUTHENTICATION_KEY = b"r" * 32
RESTORE_VERIFICATION_KEY = (
    Ed25519PrivateKey.from_private_bytes(RESTORE_VERIFICATION_AUTHENTICATION_KEY)
    .public_key()
    .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
)
RESTORE_VERIFICATION_KEY_ID = "restore-verification-test"


def _completion_marker(
    backup_id: str,
    *,
    frozen_at: str,
    additional_object_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    frozen = datetime.fromisoformat(frozen_at.replace("Z", "+00:00"))
    point_root = f"production/points/{backup_id}"
    return {
        "schema_version": 1,
        "backup_id": backup_id,
        "frozen_at": frozen.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "verified_at": (frozen + timedelta(seconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "manifest_key": f"{point_root}/manifest.json.age",
        "manifest_ciphertext_size": 512,
        "manifest_ciphertext_sha256": "f" * 64,
        "recipient_key_id": "synthetic-key",
        "generations": sorted(generation_tags(frozen, "Asia/Shanghai")),
        "object_keys": sorted(
            {
                f"{point_root}/database.dump.age",
                f"{point_root}/config.tar.age",
                f"{point_root}/manifest.json.age",
                *additional_object_keys,
            }
        ),
        "status": "remote_ciphertext_verified",
    }


def _trusted_retention_record(marker: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "backup_id": marker["backup_id"],
        "frozen_at": marker["frozen_at"],
        "generations": marker["generations"],
        "manifest_key": marker["manifest_key"],
        "object_keys": marker["object_keys"],
        "authentication_key_id": MANIFEST_AUTHENTICATION_KEY_ID,
        "manifest_sha256": "e" * 64,
        "source": "ed25519_authenticated_manifest",
    }


def _manifest_verification_key_path(tmp_path: Path) -> Path:
    path = tmp_path / "manifest-verification.key"
    path.write_bytes(MANIFEST_VERIFICATION_KEY)
    path.chmod(0o600)
    return path


def _restore_verification_key_path(tmp_path: Path) -> Path:
    path = tmp_path / "restore-verification.key"
    path.write_bytes(RESTORE_VERIFICATION_KEY)
    path.chmod(0o600)
    return path


def _signed_restore_manifest(
    backup_id: str,
    *,
    signing_key: bytes = MANIFEST_AUTHENTICATION_KEY,
) -> dict[str, Any]:
    return authenticate_manifest(
        {
            "schema_version": 1,
            "backup_id": backup_id,
            "started_at": "2026-08-07T02:30:00.000001Z",
            "frozen_at": "2026-08-07T02:30:01.000001Z",
            "completed_at": "2026-08-07T02:30:02.000001Z",
            "source_commit": "a" * 40,
            "images": {"app": f"app:v1@sha256:{'1' * 64}"},
            "config_hashes": {"compose.prod.yaml": "2" * 64},
            "alembic_revision": "20260804_0002",
            "tools": {
                "age": "1.3.1",
                "pg_dump": "pg_dump (PostgreSQL) 16.14",
                "rclone": "rclone v1.74.1",
            },
            "volume_name": "product_pdf_qr_files",
            "database_name": "synthetic",
            "recipient_key_id": "synthetic-key",
            "objects": [
                {
                    "name": name,
                    "key": f"synthetic/points/{backup_id}/{name}.age",
                    "backup_id": backup_id,
                    "plaintext_size": 10,
                    "plaintext_sha256": digest,
                    "ciphertext_size": 20,
                    "ciphertext_sha256": digest,
                }
                for name, digest in (("database", "3" * 64), ("config", "4" * 64))
            ],
            "files": [],
        },
        key=signing_key,
        key_id=MANIFEST_AUTHENTICATION_KEY_ID,
    )


def _signed_restore_verification(backup_id: str) -> dict[str, Any]:
    return authenticate_restore_verification(
        {
            "schema_version": 1,
            "backup_id": backup_id,
            "restore_operation_id": "1" * 64,
            "environment_id": "synthetic-environment",
            "verified_at": "2026-08-07T03:00:00.000001Z",
            "rule": "remote_download_plus_complete_isolated_restore_only",
            "restore_history_sha256": "2" * 64,
            "manifest_sha256": "e" * 64,
        },
        key=RESTORE_VERIFICATION_AUTHENTICATION_KEY,
        key_id=RESTORE_VERIFICATION_KEY_ID,
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_cas_split_publication_resumes_after_metadata_upload_interruption(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    state_root = tmp_path / "state"
    remote_root = tmp_path / "remote"
    source_root.mkdir()
    state_root.mkdir()
    remote_root.mkdir()
    source = source_root / "document.pdf"
    source.write_bytes(b"validated-pdf")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    class InterruptingRemote(LocalRemote):
        fail_metadata_once = True

        def upload_stream(self, source: Any, key: str) -> tuple[int, str]:
            if key.endswith(".json") and self.fail_metadata_once:
                self.fail_metadata_once = False
                raise SafetyError("synthetic metadata interruption")
            return super().upload_stream(source, key)

    class FakeCipher:
        calls = 0

        def encrypt_file_to_path(self, plaintext: Path, target: Path) -> tuple[int, str, int, str]:
            self.calls += 1
            payload = b"ciphertext:" + plaintext.read_bytes()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            return (
                plaintext.stat().st_size,
                hashlib.sha256(plaintext.read_bytes()).hexdigest(),
                len(payload),
                hashlib.sha256(payload).hexdigest(),
            )

    builder = object.__new__(BackupBuilder)
    builder.remote = InterruptingRemote(remote_root)
    builder.source_root = source_root
    builder.state_root = state_root
    builder.prefix = "production"
    builder.recipient_key_id = "synthetic-key"
    builder.cipher = cast(Any, FakeCipher())
    item = {"path": "document.pdf", "size": source.stat().st_size, "sha256": digest}

    with pytest.raises(SafetyError, match="metadata interruption"):
        builder._publish_file_object(item)
    assert builder.remote.exists(builder._cas_key(digest))
    assert not builder.remote.exists(builder._cas_meta_key(digest))
    # Simulate a hard stop after the durable ciphertext replace but before the
    # metadata checkpoint replace; retry must derive metadata without re-encrypting.
    (builder._cas_stage_root(digest) / "metadata.json").unlink()

    published = builder._publish_file_object(item)

    assert published["plaintext_sha256"] == digest
    assert cast(Any, builder.cipher).calls == 1
    assert builder.remote.exists(builder._cas_meta_key(digest))
    assert not builder._cas_stage_root(digest).exists()


def _run_backup_finalizer_wrapper(
    tmp_path: Path,
    *,
    stop_exit: int,
    deadline_after_restart: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str]:
    repository = tmp_path / "repository"
    backup_scripts = repository / "scripts" / "backup_recovery"
    production_scripts = repository / "scripts" / "production"
    backup_scripts.mkdir(parents=True)
    production_scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/backup_recovery/backup-run.sh", backup_scripts / "backup-run.sh")
    shutil.copy2(ROOT / "scripts/backup_recovery/lock.sh", backup_scripts / "lock.sh")
    for name in ("compose.prod.yaml", "compose.backup.yaml"):
        (repository / name).write_text("services: {}\n", encoding="utf-8")
    for name in (".env.prod", ".env.backup"):
        path = repository / name
        path.write_text("SYNTHETIC=1\n", encoding="utf-8")
        path.chmod(0o600)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    service_state = tmp_path / "app.state"
    service_state.write_text("healthy\n", encoding="utf-8")
    restart_marker = tmp_path / "restart.marker"
    operation_log = tmp_path / "operations.log"
    date_started = tmp_path / "date.started"

    _write_executable(
        fake_bin / "git",
        """#!/bin/sh
case "$*" in
  *"status --porcelain"*) exit 0 ;;
  *"rev-parse HEAD"*)
    printf '%s\\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    exit 0
    ;;
esac
exit 1
""",
    )
    _write_executable(
        production_scripts / "prod-compose.sh",
        """#!/bin/sh
set -eu
case "${1:-}" in
  stop)
    printf 'stopped\\n' >"$FAKE_SERVICE_STATE"
    printf 'stop\\n' >>"$FAKE_OPERATION_LOG"
    exit "$FAKE_STOP_EXIT"
    ;;
  start)
    printf 'healthy\\n' >"$FAKE_SERVICE_STATE"
    : >"$FAKE_RESTART_MARKER"
    printf 'start:%s\\n' "$*" >>"$FAKE_OPERATION_LOG"
    ;;
  *)
    exit 1
    ;;
esac
""",
    )
    _write_executable(
        fake_bin / "date",
        """#!/bin/sh
if [ ! -e "$FAKE_DATE_STARTED" ]; then
  : >"$FAKE_DATE_STARTED"
  printf '1000\\n'
elif [ "$FAKE_DEADLINE_AFTER_RESTART" = "1" ] && [ -e "$FAKE_RESTART_MARKER" ]; then
  printf '1901\\n'
else
  printf '1001\\n'
fi
""",
    )
    _write_executable(
        fake_bin / "sleep",
        "#!/bin/sh\nexit 0\n",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/bin/sh
set -eu
case "${1:-}" in
  compose)
    shift
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --env-file|-f|--profile) shift 2 ;;
        *) break ;;
      esac
    done
    command="${1:-}"
    shift || true
    case "$command" in
      ps)
        case "$*" in
          *"-q app"*) printf 'app-container\\n' ;;
          *"--status running --services migrate"*) : ;;
          *) exit 1 ;;
        esac
        ;;
      run) : ;;
      *) exit 1 ;;
    esac
    ;;
  inspect)
    case "$*" in
      *"State.Running"*"app-container"*)
        [ "$(cat "$FAKE_SERVICE_STATE")" = "stopped" ] &&
          printf 'false\\n' || printf 'true\\n'
        ;;
      *"State.Health"*"app-container"*)
        [ "$(cat "$FAKE_SERVICE_STATE")" = "healthy" ] &&
          printf 'healthy\\n' || printf 'starting\\n'
        ;;
      *"product_pdf_qr_backup_job"*) exit 1 ;;
      *) exit 1 ;;
    esac
    ;;
  network)
    printf 'product-pdf-qr-prod-db-1 \\n'
    ;;
  rm) : ;;
  exec) printf '0\\n' ;;
  *) exit 1 ;;
esac
""",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "PR2A_LOCK_DIRECTORY": str(tmp_path / "pr2a.lock"),
            "FAKE_SERVICE_STATE": str(service_state),
            "FAKE_RESTART_MARKER": str(restart_marker),
            "FAKE_OPERATION_LOG": str(operation_log),
            "FAKE_DATE_STARTED": str(date_started),
            "FAKE_STOP_EXIT": str(stop_exit),
            "FAKE_DEADLINE_AFTER_RESTART": "1" if deadline_after_restart else "0",
        }
    )
    result = subprocess.run(
        [str(backup_scripts / "backup-run.sh"), "finalize"],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, operation_log.read_text(encoding="utf-8")


def _run_restore_pre_destructive_failure_wrapper(
    tmp_path: Path,
    *,
    initial_app_state: str,
    initial_proxy_state: str,
    recovered_proxy_state: str = "healthy",
) -> tuple[subprocess.CompletedProcess[str], list[str], str, str]:
    repository = tmp_path / "repository"
    backup_scripts = repository / "scripts" / "backup_recovery"
    production_scripts = repository / "scripts" / "production"
    backup_scripts.mkdir(parents=True)
    production_scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/backup_recovery/restore-run.sh", backup_scripts / "restore-run.sh")
    shutil.copy2(ROOT / "scripts/backup_recovery/lock.sh", backup_scripts / "lock.sh")
    for name in ("compose.prod.yaml", "compose.backup.yaml"):
        (repository / name).write_text("services: {}\n", encoding="utf-8")
    for name in (".env.prod", ".env.backup"):
        path = repository / name
        path.write_text("SYNTHETIC=1\n", encoding="utf-8")
        path.chmod(0o600)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    app_state = tmp_path / "app.state"
    proxy_state = tmp_path / "proxy.state"
    operation_log = tmp_path / "operations.log"
    app_state.write_text(f"{initial_app_state}\n", encoding="utf-8")
    proxy_state.write_text(f"{initial_proxy_state}\n", encoding="utf-8")

    _write_executable(
        fake_bin / "git",
        """#!/bin/sh
case "$*" in
  *"status --porcelain"*) exit 0 ;;
  *"rev-parse HEAD"*)
    printf '%s\\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    exit 0
    ;;
esac
exit 1
""",
    )
    _write_executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
    _write_executable(
        production_scripts / "prod-compose.sh",
        """#!/bin/sh
set -eu
action="${1:-}"
shift || true
case "$action" in
  stop)
    services=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --timeout) shift 2 ;;
        app|proxy)
          services="$services $1"
          if [ "$1" = "app" ]; then
            printf 'stopped\\n' >"$FAKE_APP_STATE"
          else
            printf 'stopped\\n' >"$FAKE_PROXY_STATE"
          fi
          shift
          ;;
        *) exit 1 ;;
      esac
    done
    printf 'stop:%s\\n' "${services# }" >>"$FAKE_OPERATION_LOG"
    ;;
  start)
    for service in "$@"; do
      printf 'start:%s\\n' "$service" >>"$FAKE_OPERATION_LOG"
      if [ "$service" = "app" ]; then
        printf 'healthy\\n' >"$FAKE_APP_STATE"
      elif [ "$service" = "proxy" ]; then
        printf '%s\\n' "$FAKE_RECOVERED_PROXY_STATE" >"$FAKE_PROXY_STATE"
      else
        exit 1
      fi
    done
    ;;
  *) exit 1 ;;
esac
""",
    )
    _write_executable(
        backup_scripts / "emit-alert.sh",
        '#!/bin/sh\nprintf \'alert:%s\\n\' "$1" >>"$FAKE_OPERATION_LOG"\n',
    )
    _write_executable(
        fake_bin / "docker",
        """#!/bin/sh
set -eu
case "${1:-}" in
  compose)
    shift
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --env-file|-f|--profile) shift 2 ;;
        *) break ;;
      esac
    done
    command="${1:-}"
    shift || true
    case "$command" in
      ps)
        case "$*" in
          *"-q app"*) printf 'app-container\\n' ;;
          *"-q proxy"*) printf 'proxy-container\\n' ;;
          *) exit 1 ;;
        esac
        ;;
      run)
        case "$*" in
          *"restore retain-site"*) exit 23 ;;
          *) exit 0 ;;
        esac
        ;;
      *) exit 1 ;;
    esac
    ;;
  inspect)
    case "$*" in
      *"State.Running"*"app-container"*)
        [ "$(cat "$FAKE_APP_STATE")" = "stopped" ] &&
          printf 'false\\n' || printf 'true\\n'
        ;;
      *"State.Running"*"proxy-container"*)
        [ "$(cat "$FAKE_PROXY_STATE")" = "stopped" ] &&
          printf 'false\\n' || printf 'true\\n'
        ;;
      *"State.Health"*"app-container"*) cat "$FAKE_APP_STATE" ;;
      *"State.Health"*"proxy-container"*) cat "$FAKE_PROXY_STATE" ;;
      *) exit 1 ;;
    esac
    ;;
  *) exit 1 ;;
esac
""",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "PR2A_LOCK_DIRECTORY": str(tmp_path / "pr2a.lock"),
            "FAKE_APP_STATE": str(app_state),
            "FAKE_PROXY_STATE": str(proxy_state),
            "FAKE_OPERATION_LOG": str(operation_log),
            "FAKE_RECOVERED_PROXY_STATE": recovered_proxy_state,
        }
    )
    backup_id = "20260807T023000Z-" + "a" * 32
    result = subprocess.run(
        [str(backup_scripts / "restore-run.sh"), backup_id],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    operations = (
        operation_log.read_text(encoding="utf-8").splitlines() if operation_log.exists() else []
    )
    return (
        result,
        operations,
        app_state.read_text(encoding="utf-8").strip(),
        proxy_state.read_text(encoding="utf-8").strip(),
    )


def _restore_engine_shell(tmp_path: Path, backup_id: str) -> RestoreEngine:
    state_root = tmp_path / "state"
    remote_root = tmp_path / "remote"
    file_root = tmp_path / "files"
    state_root.mkdir()
    remote_root.mkdir()
    file_root.mkdir()
    engine = object.__new__(RestoreEngine)
    engine.state_root = state_root
    engine.remote = LocalRemote(remote_root)
    engine.file_root = file_root
    engine.prefix = "synthetic"
    engine.recipient = "age1synthetic"
    engine.recipient_key_id = "synthetic-key"
    engine.identity = tmp_path / "identity.txt"
    engine.manifest_verification_key = MANIFEST_VERIFICATION_KEY
    engine.manifest_authentication_key_id = MANIFEST_AUTHENTICATION_KEY_ID
    engine.restore_verification_authentication_key = RESTORE_VERIFICATION_AUTHENTICATION_KEY
    engine.restore_verification_authentication_key_id = RESTORE_VERIFICATION_KEY_ID
    engine.environment_id = "synthetic-environment"
    engine.guard = RestoreGuard(
        environment_id="synthetic-environment",
        backup_id=backup_id,
        operator_id="synthetic-operator",
        approved_data_loss_window="synthetic",
        authorization_record="synthetic-change",
        expires_at=datetime(2100, 1, 1, tzinfo=UTC),
        challenge="synthetic-challenge",
    )
    engine.contract = {
        "capacity_baseline": {
            "safety_margin_percent": 25,
            "rto_hard_limit_seconds": 14_400,
        }
    }
    return engine


def _publish_restore_manifest(
    engine: RestoreEngine,
    backup_id: str,
    ciphertext: bytes,
) -> None:
    manifest_key = f"synthetic/points/{backup_id}/manifest.json.age"
    size, digest = engine.remote.upload_stream(io.BytesIO(ciphertext), manifest_key)
    completion = {
        "backup_id": backup_id,
        "status": "remote_ciphertext_verified",
        "manifest_key": manifest_key,
        "manifest_ciphertext_size": size,
        "manifest_ciphertext_sha256": digest,
    }
    engine.remote.upload_stream(
        io.BytesIO(canonical_json(completion)),
        engine._completion_key(backup_id),
    )


def test_restore_manifest_reload_accepts_valid_sender_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_id = "20260807T023000Z-" + "a" * 32
    engine = _restore_engine_shell(tmp_path, backup_id)
    manifest = _signed_restore_manifest(backup_id)
    _publish_restore_manifest(engine, backup_id, b"synthetic-manifest-ciphertext")
    monkeypatch.setattr(
        restore_module,
        "decrypt_small",
        lambda _ciphertext, _identity: canonical_json(manifest),
    )

    assert engine._manifest(backup_id) == manifest


def test_offline_validation_requires_every_database_referenced_pdf_identity() -> None:
    digest = "a" * 64
    storage_path = f"aa/aa/{digest}.pdf"
    manifest = {
        "files": [
            {
                "path": f"files/{storage_path}",
                "size": 17,
                "sha256": digest,
            }
        ]
    }
    restore_module._validate_database_file_references(
        manifest,
        [
            {
                "storage_path": storage_path,
                "size_bytes": 17,
                "sha256": digest,
            }
        ],
    )

    with pytest.raises(SafetyError, match="missing or has a different identity"):
        restore_module._validate_database_file_references(
            manifest,
            [
                {
                    "storage_path": f"bb/bb/{'b' * 64}.pdf",
                    "size_bytes": 17,
                    "sha256": "b" * 64,
                }
            ],
        )
    with pytest.raises(SafetyError, match="missing or has a different identity"):
        restore_module._validate_database_file_references(
            manifest,
            [
                {
                    "storage_path": storage_path,
                    "size_bytes": 18,
                    "sha256": digest,
                }
            ],
        )


def test_restore_preflight_rejects_forged_sender_signature_before_target_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_id = "20260807T023000Z-" + "a" * 32
    engine = _restore_engine_shell(tmp_path, backup_id)
    manifest = _signed_restore_manifest(backup_id, signing_key=b"u" * 32)
    _publish_restore_manifest(engine, backup_id, b"forged-manifest-ciphertext")
    monkeypatch.setattr(
        restore_module,
        "decrypt_small",
        lambda _ciphertext, _identity: canonical_json(manifest),
    )
    deployment_check_called = False

    def deployment_check(_manifest: dict[str, Any]) -> None:
        nonlocal deployment_check_called
        deployment_check_called = True

    monkeypatch.setattr(engine, "_validate_manifest_deployment_identity", deployment_check)

    with pytest.raises(SafetyError, match="manifest authentication failed"):
        engine.preflight(backup_id)

    assert deployment_check_called is False
    assert engine._read_state(backup_id)["stage"] == "declared"


class _BoundedReader(io.BytesIO):
    largest_read = 0

    def read(self, size: int | None = -1, /) -> bytes:
        assert size is not None and 0 < size <= 1024 * 1024
        self.largest_read = max(self.largest_read, size)
        return super().read(size)


class _StreamingProcess:
    def __init__(self, payload: bytes) -> None:
        self.stdout = _BoundedReader(payload)
        self.returncode = 0

    def wait(self) -> int:
        return self.returncode


def test_remote_digest_verification_is_chunked_and_memory_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "rclone.conf"
    config.write_text("[synthetic]\ntype = s3\n", encoding="utf-8")
    config.chmod(0o600)
    payload = b"0123456789abcdef" * (1024 * 1024)
    process = _StreamingProcess(payload)

    def fake_popen(*_args: Any, **_kwargs: Any) -> _StreamingProcess:
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    remote = RcloneRemote("synthetic:bucket", config)

    assert remote.size_and_sha256("database.dump.age") == (
        len(payload),
        hashlib.sha256(payload).hexdigest(),
    )
    assert process.stdout.largest_read == 1024 * 1024


def test_composed_cleanup_releases_lock_and_preserves_exit_status(tmp_path: Path) -> None:
    lock_directory = tmp_path / "pr2a.lock"
    script = f"""
set -eu
PR2A_LOCK_DIRECTORY='{lock_directory}'
export PR2A_LOCK_DIRECTORY
. '{ROOT / "scripts/backup_recovery/lock.sh"}'
acquire_pr2a_lock
cleanup() {{
  status=$?
  trap - EXIT HUP INT TERM
  release_pr2a_lock || status=1
  exit "$status"
}}
trap cleanup EXIT HUP INT TERM
exit 23
"""

    result = subprocess.run(["sh", "-c", script], check=False)

    assert result.returncode == 23
    assert not lock_directory.exists()


def test_backup_stop_failure_recovers_app_when_stop_partially_succeeds(tmp_path: Path) -> None:
    result, operations = _run_backup_finalizer_wrapper(tmp_path, stop_exit=7)

    assert result.returncode == 7
    assert operations.splitlines() == ["stop", "start:start app"]
    assert not (tmp_path / "pr2a.lock").exists()


def test_backup_stop_window_is_checked_after_restart_health(tmp_path: Path) -> None:
    result, operations = _run_backup_finalizer_wrapper(
        tmp_path,
        stop_exit=0,
        deadline_after_restart=True,
    )

    assert result.returncode == 2
    assert operations.splitlines() == ["stop", "start:start app"]
    assert "15-minute stop window exceeded" in result.stderr
    assert not (tmp_path / "pr2a.lock").exists()


def test_pre_destructive_failure_does_not_start_intentionally_stopped_services(
    tmp_path: Path,
) -> None:
    result, operations, app_state, proxy_state = _run_restore_pre_destructive_failure_wrapper(
        tmp_path,
        initial_app_state="stopped",
        initial_proxy_state="stopped",
    )

    assert result.returncode == 23
    assert operations == ["stop:proxy app"]
    assert (app_state, proxy_state) == ("stopped", "stopped")
    assert not (tmp_path / "pr2a.lock").exists()


def test_pre_destructive_recovery_never_leaves_unhealthy_proxy_published(
    tmp_path: Path,
) -> None:
    result, operations, app_state, proxy_state = _run_restore_pre_destructive_failure_wrapper(
        tmp_path,
        initial_app_state="healthy",
        initial_proxy_state="healthy",
        recovered_proxy_state="unhealthy",
    )

    assert result.returncode == 1
    assert operations[:3] == ["stop:proxy app", "start:app", "start:proxy"]
    assert operations[3].startswith("stop:proxy")
    assert operations[4].startswith("alert:restore-pre-destructive-service-recovery-failed:")
    assert app_state == "healthy"
    assert proxy_state == "stopped"
    assert not (tmp_path / "pr2a.lock").exists()


def test_atomic_checkpoint_ignores_pid_stale_temp_and_fsyncs_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = tmp_path / "restore-state.json"
    stale = tmp_path / ".restore-state.json.1.tmp"
    stale.write_bytes(b"hard-interruption")
    fsync_types: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        fsync_types.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    atomic_write(checkpoint, b'{"stage":"database_restored"}')

    assert checkpoint.read_bytes() == b'{"stage":"database_restored"}'
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
    assert stale.read_bytes() == b"hard-interruption"
    assert any(stat.S_ISREG(mode) for mode in fsync_types)
    assert any(stat.S_ISDIR(mode) for mode in fsync_types)
    assert sorted(tmp_path.glob(".restore-state.json.*.tmp")) == [stale]


def test_local_remote_upload_ignores_pid_stale_temp_and_fsyncs_parent(
    tmp_path: Path,
) -> None:
    remote = LocalRemote(tmp_path)
    parent = tmp_path / "points"
    parent.mkdir()
    stale = parent / ".database.dump.age.1.uploading"
    stale.write_bytes(b"hard-interruption")

    size, digest = remote.upload_stream(io.BytesIO(b"ciphertext"), "points/database.dump.age")

    assert (size, digest) == (10, hashlib.sha256(b"ciphertext").hexdigest())
    assert (parent / "database.dump.age").read_bytes() == b"ciphertext"
    assert stale.read_bytes() == b"hard-interruption"
    assert sorted(parent.glob(".database.dump.age.*.uploading")) == [stale]


def test_verified_marker_requires_restore_only_signature_and_manifest_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_id = "20250101T000000Z-" + "a" * 32
    marker = _trusted_retention_record(
        _completion_marker(backup_id, frozen_at="2025-01-01T00:00:00Z")
    )
    verification = _signed_restore_verification(backup_id)
    monkeypatch.setattr(
        rotate_module,
        "_list_files",
        lambda *_args, **_kwargs: [f"{backup_id}.json"],
    )
    monkeypatch.setattr(
        rotate_module,
        "_read_json",
        lambda *_args, **_kwargs: verification,
    )

    assert rotate_module._validated_verified_ids(
        tmp_path / "unused",
        base="delete:bucket/production",
        all_markers={backup_id: marker},
        restore_verification_key=RESTORE_VERIFICATION_KEY,
        restore_verification_key_id=RESTORE_VERIFICATION_KEY_ID,
    ) == {backup_id}

    forged = _signed_restore_verification(backup_id)
    forged["manifest_sha256"] = "f" * 64
    monkeypatch.setattr(
        rotate_module,
        "_read_json",
        lambda *_args, **_kwargs: forged,
    )
    with pytest.raises(SafetyError, match="authentication failed"):
        rotate_module._validated_verified_ids(
            tmp_path / "unused",
            base="delete:bucket/production",
            all_markers={backup_id: marker},
            restore_verification_key=RESTORE_VERIFICATION_KEY,
            restore_verification_key_id=RESTORE_VERIFICATION_KEY_ID,
        )


def test_verified_listing_failure_aborts_before_any_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "delete-rclone.conf"
    config.write_text("[delete]\ntype = s3\n", encoding="utf-8")
    config.chmod(0o600)
    backup_id = "20250101T000000Z-" + "a" * 32
    marker = _completion_marker(backup_id, frozen_at="2025-01-01T00:00:00Z")
    calls: list[tuple[str, ...]] = []

    def fake_rclone(_config: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[0] == "cat":
            return subprocess.CompletedProcess(arguments, 0, json.dumps(marker), "")
        if "/complete" in arguments[1]:
            return subprocess.CompletedProcess(arguments, 0, f"{backup_id}.json\n", "")
        if "/verified" in arguments[1]:
            return subprocess.CompletedProcess(arguments, 9, "", "synthetic listing failure")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setenv("PR2A_DELETE_AUTHORIZED_ENVIRONMENT", "1")
    monkeypatch.setattr(rotate_module, "_rclone", fake_rclone)
    monkeypatch.setattr(
        rotate_module,
        "load_contract",
        lambda _path: {"business_timezone": "Asia/Shanghai"},
    )
    monkeypatch.setattr(
        rotate_module,
        "_authenticated_retention_record",
        lambda *_args, **_kwargs: _trusted_retention_record(marker),
    )

    with pytest.raises(SafetyError, match="cannot list verified"):
        rotate_module.rotate(
            config=config,
            remote="delete:bucket",
            prefix="production",
            identity=config,
            manifest_verification_key_path=_manifest_verification_key_path(tmp_path),
            manifest_authentication_key_id=MANIFEST_AUTHENTICATION_KEY_ID,
            restore_verification_key_path=_restore_verification_key_path(tmp_path),
            restore_verification_key_id=RESTORE_VERIFICATION_KEY_ID,
            contract_path=tmp_path / "unused.json",
            now=datetime(2026, 8, 7, tzinfo=UTC),
            dry_run=False,
        )

    assert not any(arguments[0] == "deletefile" for arguments in calls)


def test_two_expired_verified_points_still_preserve_newest_recovery_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "delete-rclone.conf"
    config.write_text("[delete]\ntype = s3\n", encoding="utf-8")
    config.chmod(0o600)
    older_id = "20250101T000000Z-" + "a" * 32
    newer_id = "20250102T000000Z-" + "b" * 32
    markers = {
        older_id: _completion_marker(older_id, frozen_at="2025-01-01T00:00:00Z"),
        newer_id: _completion_marker(newer_id, frozen_at="2025-01-02T00:00:00Z"),
    }

    def fake_rclone(_config: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        if arguments[0] == "cat":
            backup_id = Path(arguments[1]).stem
            return subprocess.CompletedProcess(arguments, 0, json.dumps(markers[backup_id]), "")
        if "/complete" in arguments[1]:
            return subprocess.CompletedProcess(
                arguments, 0, f"{older_id}.json\n{newer_id}.json\n", ""
            )
        if "/verified" in arguments[1]:
            return subprocess.CompletedProcess(
                arguments, 0, f"{older_id}.json\n{newer_id}.json\n", ""
            )
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setenv("PR2A_DELETE_AUTHORIZED_ENVIRONMENT", "1")
    monkeypatch.setattr(rotate_module, "_rclone", fake_rclone)
    monkeypatch.setattr(
        rotate_module,
        "load_contract",
        lambda _path: {"business_timezone": "Asia/Shanghai"},
    )
    monkeypatch.setattr(
        rotate_module,
        "_authenticated_retention_record",
        lambda *_args, **kwargs: _trusted_retention_record(markers[kwargs["backup_id"]]),
    )
    monkeypatch.setattr(
        rotate_module,
        "_validated_verified_ids",
        lambda *_args, **_kwargs: {older_id, newer_id},
    )

    result = rotate_module.rotate(
        config=config,
        remote="delete:bucket",
        prefix="production",
        identity=config,
        manifest_verification_key_path=_manifest_verification_key_path(tmp_path),
        manifest_authentication_key_id=MANIFEST_AUTHENTICATION_KEY_ID,
        restore_verification_key_path=_restore_verification_key_path(tmp_path),
        restore_verification_key_id=RESTORE_VERIFICATION_KEY_ID,
        contract_path=tmp_path / "unused.json",
        now=datetime(2026, 8, 7, tzinfo=UTC),
        dry_run=True,
    )

    assert result["decisions"][older_id] == "delete:expired_all"
    assert result["decisions"][newer_id] == "keep:unique_verified"
    assert f"delete:bucket/production/verified/{older_id}.json" in result["delete_targets"]
    assert not any(newer_id in target for target in result["delete_targets"])


def test_partial_retention_deletion_resumes_from_durable_remote_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "delete-rclone.conf"
    config.write_text("[delete]\ntype = s3\n", encoding="utf-8")
    config.chmod(0o600)
    older_id = "20250101T000000Z-" + "a" * 32
    newer_id = "20250102T000000Z-" + "b" * 32
    older_object = f"delete:bucket/production/points/{older_id}/database.dump.age"
    newer_object = f"delete:bucket/production/points/{newer_id}/database.dump.age"
    markers = {
        older_id: _completion_marker(older_id, frozen_at="2025-01-01T00:00:00Z"),
        newer_id: _completion_marker(newer_id, frozen_at="2025-01-02T00:00:00Z"),
    }
    remote_objects: dict[str, str] = {
        f"delete:bucket/production/complete/{backup_id}.json": json.dumps(marker)
        for backup_id, marker in markers.items()
    }
    remote_objects.update(
        {
            f"delete:bucket/production/verified/{older_id}.json": "{}",
            f"delete:bucket/production/verified/{newer_id}.json": "{}",
            older_object: "older-ciphertext",
            newer_object: "newer-ciphertext",
            (f"delete:bucket/{markers[older_id]['manifest_key']}"): "older-manifest-ciphertext",
            (f"delete:bucket/{markers[newer_id]['manifest_key']}"): "newer-manifest-ciphertext",
        }
    )
    fail_older_object_once = [True]

    def fake_rclone(_config: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        command = arguments[0]
        if command == "lsf":
            parent = arguments[1].rstrip("/") + "/"
            names = sorted(
                key.removeprefix(parent)
                for key in remote_objects
                if key.startswith(parent) and "/" not in key.removeprefix(parent)
            )
            stdout = "\n".join(names) + ("\n" if names else "")
            return subprocess.CompletedProcess(arguments, 0, stdout, "")
        if command == "cat":
            target = arguments[1]
            if target not in remote_objects:
                return subprocess.CompletedProcess(arguments, 1, "", "not found")
            return subprocess.CompletedProcess(arguments, 0, remote_objects[target], "")
        if command == "copyto":
            source, target = arguments[1:3]
            if target in remote_objects:
                return subprocess.CompletedProcess(arguments, 1, "", "immutable copy refused")
            if source in remote_objects:
                remote_objects[target] = remote_objects[source]
            else:
                remote_objects[target] = Path(source).read_text(encoding="utf-8")
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if command == "deletefile":
            target = arguments[1]
            if target == older_object and fail_older_object_once[0]:
                fail_older_object_once[0] = False
                return subprocess.CompletedProcess(arguments, 9, "", "transient remote failure")
            if target not in remote_objects:
                return subprocess.CompletedProcess(arguments, 1, "", "not found")
            del remote_objects[target]
            return subprocess.CompletedProcess(arguments, 0, "", "")
        raise AssertionError(arguments)

    monkeypatch.setenv("PR2A_DELETE_AUTHORIZED_ENVIRONMENT", "1")
    monkeypatch.setattr(rotate_module, "_rclone", fake_rclone)
    monkeypatch.setattr(
        rotate_module,
        "load_contract",
        lambda _path: {"business_timezone": "Asia/Shanghai"},
    )
    monkeypatch.setattr(
        rotate_module,
        "_authenticated_retention_record",
        lambda *_args, **kwargs: _trusted_retention_record(markers[kwargs["backup_id"]]),
    )
    monkeypatch.setattr(
        rotate_module,
        "_validated_verified_ids",
        lambda *_args, **_kwargs: {older_id, newer_id},
    )

    def rotate_once() -> dict[str, Any]:
        return rotate_module.rotate(
            config=config,
            remote="delete:bucket",
            prefix="production",
            identity=config,
            manifest_verification_key_path=_manifest_verification_key_path(tmp_path),
            manifest_authentication_key_id=MANIFEST_AUTHENTICATION_KEY_ID,
            restore_verification_key_path=_restore_verification_key_path(tmp_path),
            restore_verification_key_id=RESTORE_VERIFICATION_KEY_ID,
            contract_path=tmp_path / "unused.json",
            now=datetime(2026, 8, 7, tzinfo=UTC),
            dry_run=False,
        )

    with pytest.raises(SafetyError, match="authorized retention deletion failed"):
        rotate_once()

    journal = f"delete:bucket/production/deleting/{older_id}.json"
    assert journal in remote_objects
    assert json.loads(remote_objects[journal]) == _trusted_retention_record(markers[older_id])
    assert f"delete:bucket/production/complete/{older_id}.json" not in remote_objects
    assert older_object in remote_objects
    assert f"delete:bucket/production/verified/{older_id}.json" in remote_objects

    result = rotate_once()

    assert result["decisions"][older_id] == "delete:resuming"
    assert journal not in remote_objects
    assert older_object not in remote_objects
    assert f"delete:bucket/production/verified/{older_id}.json" not in remote_objects
    assert f"delete:bucket/production/complete/{newer_id}.json" in remote_objects
    assert f"delete:bucket/production/verified/{newer_id}.json" in remote_objects
    assert newer_object in remote_objects


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("control_key", "outside allowed data namespace"),
        ("other_point_key", "outside allowed data namespace"),
        ("untrusted_cas_pair", "conflicts with authenticated manifest"),
        ("noncanonical_time", "non-canonical completion frozen_at"),
        ("forged_generations", "completion generations mismatch"),
    ],
)
def test_retention_rejects_upload_controlled_delete_instructions_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    config = tmp_path / "delete-rclone.conf"
    config.write_text("[delete]\ntype = s3\n", encoding="utf-8")
    config.chmod(0o600)
    backup_id = "20250101T000000Z-" + "a" * 32
    other_id = "20250102T000000Z-" + "b" * 32
    marker = _completion_marker(backup_id, frozen_at="2025-01-01T00:00:00Z")
    if mutation == "control_key":
        marker["object_keys"] = sorted(
            [*marker["object_keys"], f"production/complete/{other_id}.json"]
        )
    elif mutation == "other_point_key":
        marker["object_keys"] = sorted(
            [*marker["object_keys"], f"production/points/{other_id}/database.dump.age"]
        )
    elif mutation == "untrusted_cas_pair":
        digest = "c" * 64
        marker["object_keys"] = sorted(
            [
                *marker["object_keys"],
                f"production/objects/sha256/{digest}/synthetic-key.age",
                f"production/objects/sha256/{digest}/synthetic-key.json",
            ]
        )
    elif mutation == "noncanonical_time":
        marker["frozen_at"] = "2025-01-01T00:00:00Z"
    elif mutation == "forged_generations":
        marker["generations"] = ["monthly:2024-01"]
    calls: list[tuple[str, ...]] = []

    def fake_rclone(_config: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[0] == "lsf":
            if "/complete" in arguments[1]:
                return subprocess.CompletedProcess(arguments, 0, f"{backup_id}.json\n", "")
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if arguments[0] == "cat":
            return subprocess.CompletedProcess(arguments, 0, json.dumps(marker), "")
        raise AssertionError(arguments)

    monkeypatch.setenv("PR2A_DELETE_AUTHORIZED_ENVIRONMENT", "1")
    monkeypatch.setattr(rotate_module, "_rclone", fake_rclone)
    monkeypatch.setattr(
        rotate_module,
        "load_contract",
        lambda _path: {"business_timezone": "Asia/Shanghai"},
    )
    trusted = _trusted_retention_record(
        _completion_marker(backup_id, frozen_at="2025-01-01T00:00:00Z")
    )
    monkeypatch.setattr(
        rotate_module,
        "_authenticated_retention_record",
        lambda *_args, **_kwargs: trusted,
    )

    with pytest.raises(SafetyError, match=message):
        rotate_module.rotate(
            config=config,
            remote="delete:bucket",
            prefix="production",
            identity=config,
            manifest_verification_key_path=_manifest_verification_key_path(tmp_path),
            manifest_authentication_key_id=MANIFEST_AUTHENTICATION_KEY_ID,
            restore_verification_key_path=_restore_verification_key_path(tmp_path),
            restore_verification_key_id=RESTORE_VERIFICATION_KEY_ID,
            contract_path=tmp_path / "unused.json",
            now=datetime(2026, 8, 7, tzinfo=UTC),
            dry_run=False,
        )

    assert not any(arguments[0] in {"copyto", "deletefile"} for arguments in calls)


def test_protected_verified_deletion_journal_fails_closed_without_republication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "delete-rclone.conf"
    config.write_text("[delete]\ntype = s3\n", encoding="utf-8")
    config.chmod(0o600)
    backup_id = "20250101T000000Z-" + "a" * 32
    marker = _completion_marker(backup_id, frozen_at="2025-01-01T00:00:00Z")
    remote_objects = {
        f"delete:bucket/production/deleting/{backup_id}.json": json.dumps(
            _trusted_retention_record(marker)
        ),
        f"delete:bucket/production/verified/{backup_id}.json": "{}",
        (f"delete:bucket/{marker['manifest_key']}"): "manifest-ciphertext",
        # Deliberately omit every other referenced data object: the journal
        # means a preceding apply may already have deleted any subset.
    }
    calls: list[tuple[str, ...]] = []

    def fake_rclone(_config: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        command = arguments[0]
        if command == "lsf":
            parent = arguments[1].rstrip("/") + "/"
            names = sorted(
                key.removeprefix(parent)
                for key in remote_objects
                if key.startswith(parent) and "/" not in key.removeprefix(parent)
            )
            return subprocess.CompletedProcess(
                arguments,
                0,
                "\n".join(names) + ("\n" if names else ""),
                "",
            )
        if command == "cat":
            return subprocess.CompletedProcess(arguments, 0, remote_objects[arguments[1]], "")
        raise AssertionError(arguments)

    monkeypatch.setenv("PR2A_DELETE_AUTHORIZED_ENVIRONMENT", "1")
    monkeypatch.setattr(rotate_module, "_rclone", fake_rclone)
    monkeypatch.setattr(
        rotate_module,
        "load_contract",
        lambda _path: {"business_timezone": "Asia/Shanghai"},
    )
    monkeypatch.setattr(
        rotate_module,
        "_authenticated_retention_record",
        lambda *_args, **_kwargs: _trusted_retention_record(marker),
    )
    monkeypatch.setattr(
        rotate_module,
        "_validated_verified_ids",
        lambda *_args, **_kwargs: {backup_id},
    )

    with pytest.raises(SafetyError, match="object integrity is unproven"):
        rotate_module.rotate(
            config=config,
            remote="delete:bucket",
            prefix="production",
            identity=config,
            manifest_verification_key_path=_manifest_verification_key_path(tmp_path),
            manifest_authentication_key_id=MANIFEST_AUTHENTICATION_KEY_ID,
            restore_verification_key_path=_restore_verification_key_path(tmp_path),
            restore_verification_key_id=RESTORE_VERIFICATION_KEY_ID,
            contract_path=tmp_path / "unused.json",
            now=datetime(2026, 8, 7, tzinfo=UTC),
            dry_run=False,
        )

    assert f"delete:bucket/production/complete/{backup_id}.json" not in remote_objects
    assert not any(arguments[0] in {"copyto", "deletefile"} for arguments in calls)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("source_commit", "source commit"),
        ("images", "image set"),
        ("config_hashes", "config set"),
    ],
)
def test_restore_preflight_binds_authenticated_manifest_to_current_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    message: str,
) -> None:
    config = tmp_path / "compose.prod.yaml"
    config.write_text("services: {}\n", encoding="utf-8")
    source_commit = "a" * 40
    images = {
        "app": f"app@sha256:{'1' * 64}",
        "db": f"db@sha256:{'2' * 64}",
        "proxy": f"proxy@sha256:{'3' * 64}",
        "certbot": f"certbot@sha256:{'4' * 64}",
        "backup_recovery": f"backup@sha256:{'5' * 64}",
    }
    for name, value in {
        "SOURCE_COMMIT": source_commit,
        "APP_IMAGE": images["app"],
        "DB_IMAGE": images["db"],
        "PROXY_IMAGE": images["proxy"],
        "CERTBOT_IMAGE": images["certbot"],
        "BACKUP_IMAGE": images["backup_recovery"],
    }.items():
        monkeypatch.setenv(name, value)
    engine = object.__new__(RestoreEngine)
    engine.repository_root = tmp_path
    engine.contract = {"recoverable_config_allowlist": ["compose.prod.yaml"]}
    manifest = engine._current_deployment_identity()
    manifest = {
        "source_commit": manifest["source_commit"],
        "images": dict(manifest["images"]),
        "config_hashes": dict(manifest["config_hashes"]),
    }
    if field == "source_commit":
        manifest[field] = "b" * 40
    elif field == "images":
        manifest[field]["app"] = f"app@sha256:{'9' * 64}"
    else:
        manifest[field]["compose.prod.yaml"] = "9" * 64

    with pytest.raises(SafetyError, match=message):
        engine._validate_manifest_deployment_identity(manifest)


def test_resume_cache_capacity_counts_only_missing_verified_ciphertext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_id = "20260807T023000Z-" + "a" * 32
    engine = _restore_engine_shell(tmp_path, backup_id)
    cached_payload = b"already-downloaded"
    missing_size = 80
    manifest = {
        "objects": [
            {
                "key": f"synthetic/points/{backup_id}/cached.age",
                "ciphertext_size": len(cached_payload),
                "ciphertext_sha256": hashlib.sha256(cached_payload).hexdigest(),
            },
            {
                "key": f"synthetic/points/{backup_id}/missing.age",
                "ciphertext_size": missing_size,
                "ciphertext_sha256": "f" * 64,
            },
        ]
    }
    cached = engine._cache_path(backup_id, str(manifest["objects"][0]["key"]))
    cached.parent.mkdir(parents=True)
    cached.write_bytes(cached_payload)

    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=99),
    )
    with pytest.raises(SafetyError, match="insufficient encrypted cache space"):
        engine._require_encrypted_cache_capacity(backup_id, manifest)

    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=100),
    )
    engine._require_encrypted_cache_capacity(backup_id, manifest)

    assert cached.read_bytes() == cached_payload


def test_target_capacity_counts_corrupt_existing_file_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_id = "20260807T023000Z-" + "e" * 32
    engine = _restore_engine_shell(tmp_path, backup_id)
    expected_payload = b"x" * 80
    target = engine.file_root / "files" / "document.pdf"
    target.parent.mkdir()
    target.write_bytes(b"corrupt-existing-file")
    manifest = {
        "files": [
            {
                "path": "files/document.pdf",
                "size": len(expected_payload),
                "sha256": hashlib.sha256(expected_payload).hexdigest(),
            }
        ]
    }

    assert engine._target_restore_requirements(manifest) == (80, 1)

    free = [99]
    inodes = [2]
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(free=free[0]))
    monkeypatch.setattr(os, "statvfs", lambda _path: SimpleNamespace(f_favail=inodes[0]))
    with pytest.raises(SafetyError, match="insufficient target file space"):
        engine._require_target_capacity(manifest, safety_percent=25)

    free[0] = 100
    inodes[0] = 0
    with pytest.raises(SafetyError, match="insufficient target inodes"):
        engine._require_target_capacity(manifest, safety_percent=25)

    inodes[0] = 1
    engine._require_target_capacity(manifest, safety_percent=25)

    temporary = target.with_name(f".{target.name}.restore")
    temporary.write_bytes(b"interrupted" * 1024)
    reclaimable_bytes, reclaimable_inodes = engine._reclaimable_restore_temporary_capacity(manifest)
    assert reclaimable_bytes >= 100
    assert reclaimable_inodes == 1
    free[0] = 0
    inodes[0] = 0
    engine._require_target_capacity(manifest, safety_percent=25)


def test_new_restore_authorization_gets_fresh_state_for_reusable_backup(
    tmp_path: Path,
) -> None:
    backup_id = "20260807T023000Z-" + "f" * 32
    engine = _restore_engine_shell(tmp_path, backup_id)
    first = engine.declare(backup_id)
    first_path = engine._state_path(backup_id)
    first_state = engine._read_state(backup_id)
    first_state["stage"] = "rolled_back"
    first_path.write_bytes(canonical_json(first_state))

    engine.guard = replace(
        engine.guard,
        authorization_record="synthetic-change-2",
        challenge="synthetic-challenge-2",
    )
    second = engine.declare(backup_id)
    second_path = engine._state_path(backup_id)

    assert second["operation_id"] != first["operation_id"]
    assert second["stage"] == "declared"
    assert second_path != first_path
    assert json.loads(first_path.read_text(encoding="utf-8"))["stage"] == "rolled_back"
    assert engine._read_state(backup_id)["stage"] == "declared"


def test_site_retention_discards_partial_staging_and_retries_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_id = "20260807T023000Z-" + "b" * 32
    engine = _restore_engine_shell(tmp_path, backup_id)
    engine.declare(backup_id)
    engine._advance(backup_id, "preflight_complete")

    class FakeAgeCipher:
        def __init__(self, _recipient: str) -> None:
            pass

        def encrypt_command(
            self, _command: list[str], remote: LocalRemote, key: str
        ) -> tuple[int, str, int, str]:
            plaintext = b"database"
            ciphertext = b"encrypted-database"
            size, digest = remote.upload_stream(io.BytesIO(ciphertext), key)
            return len(plaintext), hashlib.sha256(plaintext).hexdigest(), size, digest

        def encrypt_bytes(self, payload: bytes, remote: LocalRemote, key: str) -> tuple[int, str]:
            return remote.upload_stream(io.BytesIO(b"encrypted:" + payload), key)

    monkeypatch.setattr(restore_module, "AgeCipher", FakeAgeCipher)
    monkeypatch.setattr(
        engine,
        "_current_deployment_identity",
        lambda: {"source_commit": "a" * 40, "images": {}, "config_hashes": {}},
    )
    monkeypatch.setenv("RESTORE_FAIL_STAGE", "site_retention")

    with pytest.raises(SafetyError, match="injected restore failure"):
        engine.retain_site(backup_id)

    operation_id = engine._operation_id()
    onsite = engine.state_root / "site-retention" / backup_id / operation_id
    staging = onsite.with_name(f".{operation_id}.staging")
    assert not onsite.exists()
    assert not staging.exists()
    assert engine._read_state(backup_id)["stage"] == "preflight_complete"

    staging.mkdir(parents=True)
    (staging / "partial.age").write_bytes(b"interrupted")
    monkeypatch.delenv("RESTORE_FAIL_STAGE")
    engine.retain_site(backup_id)

    assert engine._read_state(backup_id)["stage"] == "site_retained"
    assert not staging.exists()
    assert {path.name for path in onsite.iterdir()} == {
        "database.dump.age",
        "files.json.age",
        "identity.json.age",
    }


def test_restore_rto_declaration_is_reused_and_limit_accumulates_across_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_id = "20260807T023000Z-" + "c" * 32
    engine = _restore_engine_shell(tmp_path, backup_id)
    started = datetime(2026, 8, 7, 2, 30, tzinfo=UTC)
    clock = [started]
    monkeypatch.setattr(restore_module, "utc_now", lambda: clock[0])

    first = engine.declare(backup_id)
    clock[0] = started + timedelta(hours=3)
    retry = engine.declare(backup_id)

    assert retry["declared_at"] == first["declared_at"]

    state = engine._read_state(backup_id)
    state["stage"] = "proxy_authorized"
    engine._state_path(backup_id).write_bytes(canonical_json(state))
    clock[0] = started + timedelta(hours=5)

    with pytest.raises(SafetyError, match="four-hour restore RTO exceeded"):
        engine.external_ready(backup_id)

    assert engine._read_state(backup_id)["stage"] == "proxy_authorized"
    assert not engine.remote.exists(f"synthetic/verified/{backup_id}.json")


def test_external_ready_records_persisted_elapsed_time_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_id = "20260807T023000Z-" + "d" * 32
    engine = _restore_engine_shell(tmp_path, backup_id)
    started = datetime(2026, 8, 7, 2, 30, tzinfo=UTC)
    clock = [started]
    monkeypatch.setattr(restore_module, "utc_now", lambda: clock[0])
    engine.declare(backup_id)
    state = engine._read_state(backup_id)
    state["stage"] = "proxy_authorized"
    engine._state_path(backup_id).write_bytes(canonical_json(state))
    monkeypatch.setattr(engine, "_manifest", lambda _backup_id: _signed_restore_manifest(backup_id))
    clock[0] = started + timedelta(hours=3)

    first = engine.external_ready(backup_id)
    clock[0] = started + timedelta(hours=3, minutes=30)
    retry = engine.external_ready(backup_id)

    assert first == retry == {"backup_id": backup_id, "elapsed_seconds": 10_800}
    history = engine._read_state(backup_id)["history"]
    assert [item["stage"] for item in history] == ["external_ready"]
    verification = json.loads(engine.remote.read_bytes(f"synthetic/verified/{backup_id}.json"))
    verify_restore_verification(
        verification,
        key=RESTORE_VERIFICATION_KEY,
        key_id=RESTORE_VERIFICATION_KEY_ID,
    )


def test_external_ready_reuses_authenticated_proof_for_new_restore_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_id = "20260807T023000Z-" + "e" * 32
    engine = _restore_engine_shell(tmp_path, backup_id)
    manifest = _signed_restore_manifest(backup_id)
    monkeypatch.setattr(engine, "_manifest", lambda _backup_id: manifest)

    first = engine.declare(backup_id)
    first_state = engine._read_state(backup_id)
    first_state["stage"] = "proxy_authorized"
    engine._state_path(backup_id).write_bytes(canonical_json(first_state))
    engine.external_ready(backup_id)
    marker_key = f"synthetic/verified/{backup_id}.json"
    first_marker = engine.remote.read_bytes(marker_key)

    engine.guard = replace(
        engine.guard,
        authorization_record="synthetic-change-2",
        challenge="synthetic-challenge-2",
    )
    second = engine.declare(backup_id)
    second_state = engine._read_state(backup_id)
    second_state["stage"] = "proxy_authorized"
    engine._state_path(backup_id).write_bytes(canonical_json(second_state))

    result = engine.external_ready(backup_id)

    assert second["operation_id"] != first["operation_id"]
    assert result["backup_id"] == backup_id
    assert engine._read_state(backup_id)["stage"] == "external_ready"
    assert engine.remote.read_bytes(marker_key) == first_marker
    verification = json.loads(first_marker)
    verify_restore_verification(
        verification,
        key=RESTORE_VERIFICATION_KEY,
        key_id=RESTORE_VERIFICATION_KEY_ID,
    )
    assert verification["restore_operation_id"] == first["operation_id"]
    assert verification["manifest_sha256"] == hashlib.sha256(canonical_json(manifest)).hexdigest()


def test_restore_acl_bypass_requires_explicit_double_synthetic_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "restored.pdf"
    target.write_bytes(b"restored")
    monkeypatch.setenv("BACKUP_SYNTHETIC", "1")
    calls: list[list[str]] = []

    def failed_setfacl(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 1, "", "unsupported")

    monkeypatch.setattr(subprocess, "run", failed_setfacl)
    with pytest.raises(SafetyError, match="ACL initialization"):
        crypto._set_restore_acl(target, directory=False)
    assert calls

    monkeypatch.setenv("RESTORE_SYNTHETIC_BIND_MOUNT", "1")
    calls.clear()
    crypto._set_restore_acl(target, directory=False)
    assert calls == []


def test_file_restore_discards_regular_temporary_left_by_hard_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "document.pdf"
    temporary = tmp_path / ".document.pdf.restore"
    temporary.write_bytes(b"partial-from-killed-container")

    class FakeDecryptProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"fully-restored")

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: FakeDecryptProcess())
    monkeypatch.setattr(crypto, "_set_restore_acl", lambda _path, *, directory: None)

    observed = crypto.decrypt_to_path(
        tmp_path / "ciphertext.age",
        tmp_path / "identity.txt",
        target,
    )

    assert observed == (
        len(b"fully-restored"),
        hashlib.sha256(b"fully-restored").hexdigest(),
    )
    assert target.read_bytes() == b"fully-restored"
    assert not temporary.exists()


def test_file_restore_refuses_unsafe_temporary_left_by_hard_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "document.pdf"
    temporary = tmp_path / ".document.pdf.restore"
    temporary.mkdir()
    started = False

    def unexpected_popen(*_args: Any, **_kwargs: Any) -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(subprocess, "Popen", unexpected_popen)

    with pytest.raises(SafetyError, match="restore temporary is unsafe"):
        crypto.decrypt_to_path(
            tmp_path / "ciphertext.age",
            tmp_path / "identity.txt",
            target,
        )

    assert not started
    assert temporary.is_dir()
