"""Static regression tests preserving PR1 and hardening PR2A one-shot jobs."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_pr1_five_service_compose_is_not_modified_by_backup_overlay() -> None:
    production = (ROOT / "compose.prod.yaml").read_text(encoding="utf-8")
    overlay = (ROOT / "compose.backup.yaml").read_text(encoding="utf-8")

    assert production.count("\n  proxy:") == 1
    assert production.count("\n  certbot:") == 1
    assert production.count("\n  app:") == 1
    assert production.count("\n  db:") == 1
    assert production.count("\n  migrate:") == 1
    assert "\n  backup:" not in production
    assert "\n  restore:" not in production
    assert 'profiles: ["backup"]' in overlay
    assert 'profiles: ["restore"]' in overlay


def test_backup_and_restore_runtime_constraints_are_explicit() -> None:
    overlay = (ROOT / "compose.backup.yaml").read_text(encoding="utf-8")

    assert 'user: "10002:10002"' in overlay
    assert "privileged: false" in overlay
    assert "read_only: true" in overlay
    assert "cap_drop:\n    - ALL" in overlay
    assert "no-new-privileges:true" in overlay
    assert "/var/run/docker.sock" not in overlay
    assert "network_mode: host" not in overlay
    assert "pid: host" not in overlay
    assert "ipc: host" not in overlay
    assert 'profiles: ["backup-volume-init"]' in overlay
    assert 'user: "10001:10001"' in overlay
    assert "network_mode: none" in overlay
    assert "cap_add:" not in overlay


def test_scheduled_backup_is_read_only_and_has_no_restore_secrets() -> None:
    overlay = (ROOT / "compose.backup.yaml").read_text(encoding="utf-8")
    backup = overlay.split("\n  restore:", maxsplit=1)[0]

    assert "PGUSER: app_backup" in backup
    assert "target: /data/files\n        read_only: true" in backup
    assert "BACKUP_IMAGE: ${BACKUP_IMAGE:?BACKUP_IMAGE is required}" in backup
    assert "manifest-authentication.key" in backup
    assert "BACKUP_MANIFEST_AUTHENTICATION_KEY_ID" in backup
    assert "age-identity" not in backup
    assert "app_migrate_pgpass" not in backup
    assert "RESTORE_CONFIRMATION" not in backup


def test_restore_is_default_off_and_uses_one_time_owner_secret() -> None:
    overlay = (ROOT / "compose.backup.yaml").read_text(encoding="utf-8")
    restore = overlay.split("\n  restore:", maxsplit=1)[1]

    assert "PGUSER: app_migrate" in restore
    assert "app_migrate_pgpass" in restore
    assert "age-identity.txt" in restore
    assert "target: /run/secrets/manifest-authentication.key" not in restore
    assert "manifest-verification.key" in restore
    assert "RESTORE_MANIFEST_VERIFICATION_KEY_FILE" in restore
    assert "BACKUP_MANIFEST_AUTHENTICATION_KEY_ID" in restore
    assert "restore-verification-authentication.key" in restore
    assert "RESTORE_VERIFICATION_AUTHENTICATION_KEY_ID" in restore
    backup = overlay.split("\n  restore:", maxsplit=1)[0]
    assert "restore-verification-authentication.key" not in backup
    assert "restore-authorization.json" in restore
    assert "restart: unless-stopped" not in restore


def test_locked_backup_image_contains_pg16_age_and_rclone_only() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    requirements = (ROOT / "deploy/backup/requirements.txt").read_text(encoding="utf-8")

    assert "AS backup-recovery-runtime" in dockerfile
    assert "age=1.3.1-r5" in dockerfile
    assert "postgresql16-client=16.14-r0" in dockerfile
    assert "rclone=1.74.1-r1" in dockerfile
    assert "acl=2.3.2-r1" in dockerfile
    assert "COPY deploy/backup/requirements.txt" in dockerfile
    assert "--only-binary=:all:" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "USER 10002:10002" in dockerfile
    assert "chown -R 10002:10002 /var/lib/backup" in dockerfile
    backup_stage = dockerfile.split("AS backup-recovery-runtime", maxsplit=1)[1]
    assert "tests" not in backup_stage
    assert "pytest" not in backup_stage
    assert "mypy" not in backup_stage
    locked_packages = {
        line.split("==", maxsplit=1)[0]
        for line in requirements.splitlines()
        if line and not line.startswith(("#", " ", "\t"))
    }
    assert locked_packages == {"cryptography", "cffi", "pycparser"}
    assert requirements.count("--hash=sha256:") == 7


def test_finalizer_timer_never_catches_up_during_daytime() -> None:
    finalizer = (ROOT / "deploy/production/systemd/product-pdf-qr-backup-finalize.timer").read_text(
        encoding="utf-8"
    )
    precopy = (ROOT / "deploy/production/systemd/product-pdf-qr-backup-precopy.timer").read_text(
        encoding="utf-8"
    )

    assert "OnCalendar=*-*-* 02:30:00 Asia/Shanghai" in finalizer
    assert "Persistent=false" in finalizer
    assert "OnCalendar=*-*-* 06,10,14,18,22:30:00 Asia/Shanghai" in precopy
    assert "Persistent=true" in precopy


def test_restore_script_orders_proxy_and_app_gates() -> None:
    script = (ROOT / "scripts/backup_recovery/restore-run.sh").read_text(encoding="utf-8")
    order = [
        "declare --backup-id",
        "preflight --backup-id",
        "stop --timeout 60 proxy app",
        "retain-site --backup-id",
        "restore-database --backup-id",
        "restore-files --backup-id",
        "offline-validate --backup-id",
        "start app",
        "record-functional-validation",
        "authorize-proxy",
        "start proxy",
        "external-ready",
    ]
    positions: list[int] = []
    cursor = 0
    for fragment in order:
        position = script.index(fragment, cursor)
        positions.append(position)
        cursor = position + len(fragment)

    assert positions == sorted(positions)
    assert "restore_failure" in script
    assert "prod stop --timeout 30 proxy" in script
    assert 'restore_started="$(date +%s)"' not in script
    assert '"elapsed_seconds":\\([0-9][0-9]*\\)' in script


def test_backup_script_restores_app_on_every_stopped_path() -> None:
    script = (ROOT / "scripts/backup_recovery/backup-run.sh").read_text(encoding="utf-8")
    stop_position = script.index('prod-compose.sh" stop --timeout 60 app')
    recovery_mark_position = script.rindex("app_needs_recovery=1", 0, stop_position)
    finalizer_position = script.index("run_finalizer_with_deadline", stop_position)
    restart_position = script.index(
        'restore_app || fail "app failed to become healthy',
        finalizer_position,
    )
    elapsed_position = script.index("elapsed=$(($(date +%s) - window_started))", restart_position)

    assert "trap cleanup EXIT HUP INT TERM" in script
    assert "restore_app || status=1" in script
    assert "assert_migrate_not_running" in script
    assert "assert_database_network_members" in script
    assert "assert-quiescent" in script
    assert 'elapsed" -le 900' in script
    assert recovery_mark_position < stop_position < finalizer_position < restart_position
    assert restart_position < elapsed_position
    assert "window_deadline=$((window_started + 900))" in script
    assert '"$(date +%s)" -lt "$window_deadline"' in script
    assert "release_pr2a_lock || status=1" in script


def test_persistent_runtime_temporaries_have_crash_resume_strategies() -> None:
    model = (ROOT / "scripts/backup_recovery/model.py").read_text(encoding="utf-8")
    remote = (ROOT / "scripts/backup_recovery/remote.py").read_text(encoding="utf-8")
    crypto = (ROOT / "scripts/backup_recovery/crypto.py").read_text(encoding="utf-8")
    restore = (ROOT / "scripts/backup_recovery/restore.py").read_text(encoding="utf-8")
    storage = (ROOT / "src/product_pdf_qr/domains/storage/service.py").read_text(encoding="utf-8")
    qrcode = (ROOT / "src/product_pdf_qr/domains/qrcode/service.py").read_text(encoding="utf-8")

    for publication in (model, remote):
        assert "tempfile.mkstemp(" in publication
        assert "os.getpid()" not in publication
        assert "os.fsync(directory_descriptor)" in publication
    assert 'temporary.open("xb")' in crypto
    assert "if temporary.is_file():\n        temporary.unlink()" in crypto
    assert "shutil.rmtree(staging_root, ignore_errors=True)" in restore
    assert "tempfile.mkstemp(" in storage
    assert "os.fchmod(descriptor, 0o660)" in storage
    assert 'excluded_top_level_names={"temporary"}' in (
        ROOT / "scripts/backup_recovery/backup.py"
    ).read_text(encoding="utf-8")
    assert "tempfile.mkstemp(" in qrcode
    assert "os.fchmod(file_descriptor, 0o660)" in qrcode


def test_restore_script_releases_lock_on_success_and_failure() -> None:
    script = (ROOT / "scripts/backup_recovery/restore-run.sh").read_text(encoding="utf-8")
    state_capture_position = script.index('app_initial_state="$(service_initial_state app)"')
    stop_position = script.index("prod stop --timeout 60 proxy app")
    recovery_mark_position = script.rindex("services_need_recovery=1", 0, stop_position)
    database_restore_position = script.index("restore-database --backup-id")
    destructive_mark_position = script.rindex(
        "destructive_restore_started=1",
        0,
        database_restore_position,
    )

    assert script.count("release_pr2a_lock") == 2
    assert "release_pr2a_lock || status=1" in script
    assert 'SOURCE_COMMIT="$source_commit" docker compose' in script
    assert "deployment checkout must be clean before restore" in script
    assert "trap restore_failure EXIT HUP INT TERM" in script
    assert "printf 'running_unhealthy\\n'" in script
    assert state_capture_position < recovery_mark_position < stop_position
    assert recovery_mark_position < stop_position
    assert destructive_mark_position < database_restore_position
    recovery_function_position = script.index("restore_pre_destructive_services")
    app_recovery_position = script.index("prod start app", recovery_function_position)
    app_health_position = script.index("wait_healthy app", app_recovery_position)
    proxy_recovery_position = script.index("prod start proxy", app_health_position)
    proxy_health_position = script.index("wait_healthy proxy", proxy_recovery_position)
    proxy_stop_position = script.index("prod stop --timeout 30 proxy", proxy_health_position)
    assert (
        app_recovery_position
        < app_health_position
        < proxy_recovery_position
        < proxy_health_position
        < proxy_stop_position
    )


def test_remote_upload_identity_policy_has_no_delete_permission() -> None:
    contract = json.loads((ROOT / "deploy/backup/contract.json").read_text(encoding="utf-8"))
    remote = contract["remote"]

    assert "DeleteObject" not in remote["upload_identity_permissions"]
    assert "DeleteObject" in remote["upload_identity_forbidden_permissions"]
    assert "DeleteObjectVersion" in remote["upload_identity_forbidden_permissions"]
    assert set(remote["delete_identity_permissions"]) == {
        "ListBucket",
        "GetObject",
        "PutObject",
        "DeleteObject",
    }
    assert "DeleteObjectVersion" in remote["delete_identity_forbidden_permissions"]
    assert "BypassGovernanceRetention" in remote["delete_identity_forbidden_permissions"]
    assert remote["delete_identity_location"] == (
        "independent_authorized_environment_or_storage_lifecycle"
    )


def test_delete_tool_is_not_copied_into_production_backup_image() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "scripts/retention" not in dockerfile
    assert (ROOT / "scripts/retention/rotate.py").is_file()


def test_backup_environment_is_separate_from_pr1_environment() -> None:
    production = (ROOT / ".env.prod.example").read_text(encoding="utf-8")
    backup = (ROOT / ".env.backup.example").read_text(encoding="utf-8")

    assert "BACKUP_AGE_RECIPIENT" not in production
    assert "RESTORE_AGE_IDENTITY_FILE" not in production
    assert "BACKUP_AGE_RECIPIENT" in backup
    assert "BACKUP_MANIFEST_AUTHENTICATION_KEY_FILE" in backup
    assert "BACKUP_MANIFEST_AUTHENTICATION_KEY_ID" in backup
    assert "RESTORE_AGE_IDENTITY_FILE" in backup
    assert "RESTORE_MANIFEST_VERIFICATION_KEY_FILE" in backup
    assert "AGE-SECRET-KEY-" not in backup
