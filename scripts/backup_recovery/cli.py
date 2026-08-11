"""Safe command-line entry points for backup, retention, and restore."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.backup_recovery.backup import BackupBuilder
from scripts.backup_recovery.contract import ContractError, load_contract
from scripts.backup_recovery.model import SafetyError, canonical_json, retention_decisions
from scripts.backup_recovery.remote import remote_from_environment
from scripts.backup_recovery.restore import RestoreEngine


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SafetyError(f"{name} is required")
    return value


def _remote() -> Any:
    value = _required_environment("BACKUP_REMOTE")
    synthetic = os.environ.get("BACKUP_SYNTHETIC") == "1"
    config_value = os.environ.get("RCLONE_CONFIG", "/run/secrets/rclone.conf")
    config = Path(config_value) if not value.startswith("local:") else None
    return remote_from_environment(value, synthetic=synthetic, config=config)


def _manifest_key(*, environment_name: str, default: str, description: str) -> bytes:
    path = Path(
        os.environ.get(
            environment_name,
            default,
        )
    )
    try:
        metadata = path.lstat()
        if not path.is_file() or path.is_symlink() or metadata.st_mode & 0o077:
            raise SafetyError(f"{description} must be a regular mode-0600 file")
        key = path.read_bytes()
    except OSError as error:
        raise SafetyError(f"{description} is unavailable") from error
    if len(key) != 32:
        raise SafetyError(f"{description} must be exactly 32 bytes")
    return key


def _builder(contract: dict[str, Any]) -> BackupBuilder:
    return BackupBuilder(
        contract=contract,
        remote=_remote(),
        source_root=Path(os.environ.get("BACKUP_SOURCE_ROOT", "/data/files")),
        repository_root=Path(os.environ.get("BACKUP_REPOSITORY_ROOT", "/source")),
        state_root=Path(os.environ.get("BACKUP_STATE_ROOT", "/var/lib/backup")),
        recipient=_required_environment("BACKUP_AGE_RECIPIENT"),
        recipient_key_id=_required_environment("BACKUP_RECIPIENT_KEY_ID"),
        manifest_authentication_key=_manifest_key(
            environment_name="BACKUP_MANIFEST_AUTHENTICATION_KEY",
            default="/run/secrets/manifest-authentication.key",
            description="manifest signing key",
        ),
        manifest_authentication_key_id=_required_environment(
            "BACKUP_MANIFEST_AUTHENTICATION_KEY_ID"
        ),
        remote_prefix=_required_environment("BACKUP_REMOTE_PREFIX"),
    )


def _restore(contract: dict[str, Any]) -> RestoreEngine:
    return RestoreEngine(
        contract=contract,
        remote=_remote(),
        remote_prefix=_required_environment("BACKUP_REMOTE_PREFIX"),
        state_root=Path(os.environ.get("BACKUP_STATE_ROOT", "/var/lib/backup")),
        file_root=Path(os.environ.get("BACKUP_SOURCE_ROOT", "/data/files")),
        identity=Path(os.environ.get("RESTORE_AGE_IDENTITY", "/run/secrets/age-identity.txt")),
        recipient=_required_environment("BACKUP_AGE_RECIPIENT"),
        recipient_key_id=_required_environment("BACKUP_RECIPIENT_KEY_ID"),
        manifest_verification_key=_manifest_key(
            environment_name="RESTORE_MANIFEST_VERIFICATION_KEY",
            default="/run/secrets/manifest-verification.key",
            description="manifest verification key",
        ),
        manifest_authentication_key_id=_required_environment(
            "BACKUP_MANIFEST_AUTHENTICATION_KEY_ID"
        ),
        restore_verification_authentication_key=_manifest_key(
            environment_name="RESTORE_VERIFICATION_AUTHENTICATION_KEY",
            default="/run/secrets/restore-verification-authentication.key",
            description="restore verification signing key",
        ),
        restore_verification_authentication_key_id=_required_environment(
            "RESTORE_VERIFICATION_AUTHENTICATION_KEY_ID"
        ),
        environment_id=_required_environment("RESTORE_ENVIRONMENT_ID"),
        environment_marker=Path(
            os.environ.get("RESTORE_ENVIRONMENT_MARKER", "/run/config/environment-id")
        ),
        authorization=Path(
            os.environ.get("RESTORE_AUTHORIZATION", "/run/secrets/restore-authorization.json")
        ),
        confirmation=_required_environment("RESTORE_CONFIRMATION"),
        repository_root=Path(os.environ.get("BACKUP_REPOSITORY_ROOT", "/source")),
    )


def parser() -> argparse.ArgumentParser:
    """Build the explicit command surface; no destructive default action."""

    root = argparse.ArgumentParser(prog="backup-recovery")
    root.add_argument(
        "--contract",
        type=Path,
        default=Path(os.environ.get("BACKUP_CONTRACT", "deploy/backup/contract.json")),
    )
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-contract")
    commands.add_parser("assert-quiescent")
    commands.add_parser("precopy")
    commands.add_parser("finalize")
    declare = commands.add_parser("declare")
    declare.add_argument("--backup-id", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--backup-id", required=True)
    retain = commands.add_parser("retain-site")
    retain.add_argument("--backup-id", required=True)
    database = commands.add_parser("restore-database")
    database.add_argument("--backup-id", required=True)
    files = commands.add_parser("restore-files")
    files.add_argument("--backup-id", required=True)
    offline = commands.add_parser("offline-validate")
    offline.add_argument("--backup-id", required=True)
    functional = commands.add_parser("record-functional-validation")
    functional.add_argument("--backup-id", required=True)
    functional.add_argument("--evidence", type=Path, required=True)
    proxy = commands.add_parser("authorize-proxy")
    proxy.add_argument("--backup-id", required=True)
    ready = commands.add_parser("external-ready")
    ready.add_argument("--backup-id", required=True)
    rollback = commands.add_parser("rollback-site")
    rollback.add_argument("--backup-id", required=True)
    retention = commands.add_parser("retention-plan")
    retention.add_argument("--points", type=Path, required=True)
    retention.add_argument("--now", required=True)
    retention.add_argument("--unique-verified-backup-id")
    return root


def run(arguments: list[str] | None = None) -> int:
    """Execute one bounded operation and emit non-secret JSON."""

    options = parser().parse_args(arguments)
    contract = load_contract(options.contract)
    result: Any = {"status": "ok"}
    if options.command == "validate-contract":
        result = {"status": "ok", "schema_version": contract["schema_version"]}
    elif options.command == "assert-quiescent":
        _builder(contract).assert_quiescent()
    elif options.command == "precopy":
        result = _builder(contract).precopy()
    elif options.command == "finalize":
        result = _builder(contract).finalize()
    elif options.command == "retention-plan":
        points = json.loads(options.points.read_text(encoding="utf-8"))
        now = datetime.fromisoformat(options.now.replace("Z", "+00:00")).astimezone(UTC)
        result = retention_decisions(
            points,
            now=now,
            timezone=contract["business_timezone"],
            unique_verified_backup_id=options.unique_verified_backup_id,
        )
    else:
        restore = _restore(contract)
        backup_id = options.backup_id
        if options.command == "declare":
            result = restore.declare(backup_id)
        elif options.command == "preflight":
            result = restore.preflight(backup_id)
        elif options.command == "retain-site":
            restore.retain_site(backup_id)
        elif options.command == "restore-database":
            restore.restore_database(backup_id)
        elif options.command == "restore-files":
            restore.restore_files(backup_id)
        elif options.command == "offline-validate":
            result = restore.offline_validate(backup_id)
        elif options.command == "record-functional-validation":
            restore.record_functional_validation(backup_id, options.evidence)
        elif options.command == "authorize-proxy":
            restore.authorize_proxy(backup_id)
        elif options.command == "external-ready":
            result = restore.external_ready(backup_id)
        elif options.command == "rollback-site":
            restore.rollback_site(backup_id)
    sys.stdout.buffer.write(canonical_json(result))
    return 0


def main() -> int:
    """Map every expected safety failure to a non-zero, non-secret result."""

    try:
        return run()
    except (ContractError, SafetyError, OSError, KeyError, ValueError) as error:
        print(f"backup-recovery refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
