"""Delete expired logical points with an identity absent from production."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from scripts.backup_recovery.contract import load_contract
from scripts.backup_recovery.crypto import decrypt_small
from scripts.backup_recovery.model import (
    SafetyError,
    canonical_json,
    retention_decisions,
    retention_record_from_authenticated_manifest,
    validate_backup_id,
    validate_completion_marker,
    verify_restore_verification,
)


def _rclone(config: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", "--config", str(config), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )


def _read_json(config: Path, target: str) -> dict[str, Any]:
    result = _rclone(config, "cat", target)
    if result.returncode != 0:
        raise SafetyError("authorized retention read failed")
    return cast(dict[str, Any], json.loads(result.stdout))


def _list_files(config: Path, target: str, *, description: str) -> list[str]:
    result = _rclone(config, "lsf", target, "--files-only")
    if result.returncode != 0:
        raise SafetyError(f"cannot list {description}")
    return [name for name in result.stdout.splitlines() if name]


def _remote_file_exists(config: Path, target: str) -> bool:
    parent, name = target.rsplit("/", 1)
    return name in _list_files(config, parent, description="retention deletion target")


def _delete_if_present(config: Path, target: str) -> None:
    """Delete one exact remote file idempotently, failing closed on listing errors."""

    if not _remote_file_exists(config, target):
        return
    result = _rclone(config, "deletefile", target)
    if result.returncode != 0:
        raise SafetyError("authorized retention deletion failed")


def _publish_and_verify_journal(
    config: Path,
    *,
    target: str,
    expected: dict[str, Any],
) -> None:
    with tempfile.TemporaryDirectory(prefix="pr2a-retention-") as temporary:
        source = Path(temporary) / "deletion-journal.json"
        source.write_bytes(canonical_json(expected))
        source.chmod(0o600)
        result = _rclone(config, "copyto", str(source), target, "--immutable")
        if result.returncode != 0:
            raise SafetyError("cannot publish retention deletion journal")
    if _read_json(config, target) != expected:
        raise SafetyError("retention deletion journal verification failed")


def _authenticated_retention_record(
    config: Path,
    *,
    remote: str,
    prefix: str,
    backup_id: str,
    identity: Path,
    timezone: str,
    manifest_verification_key: bytes,
    manifest_authentication_key_id: str,
) -> dict[str, Any]:
    manifest_key = f"{prefix}/points/{backup_id}/manifest.json.age"
    source = f"{remote.rstrip('/')}/{manifest_key}"
    with tempfile.TemporaryDirectory(prefix="pr2a-retention-") as temporary:
        ciphertext = Path(temporary) / "manifest.json.age"
        result = _rclone(config, "copyto", source, str(ciphertext))
        if result.returncode != 0:
            raise SafetyError("cannot download authoritative encrypted manifest")
        try:
            manifest = json.loads(decrypt_small(ciphertext, identity))
        except json.JSONDecodeError as error:
            raise SafetyError("authenticated retention manifest is invalid") from error
    if not isinstance(manifest, dict):
        raise SafetyError("authenticated retention manifest is not a JSON object")
    return retention_record_from_authenticated_manifest(
        manifest,
        backup_id=backup_id,
        prefix=prefix,
        timezone=timezone,
        verification_key=manifest_verification_key,
        authentication_key_id=manifest_authentication_key_id,
    )


def _validated_completion_records(
    config: Path,
    *,
    remote: str,
    base: str,
    prefix: str,
    identity: Path,
    timezone: str,
    manifest_verification_key: bytes,
    manifest_authentication_key_id: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name in _list_files(
        config,
        f"{base}/complete",
        description="completion markers",
    ):
        if not name.endswith(".json"):
            continue
        backup_id = name.removesuffix(".json")
        validate_backup_id(backup_id)
        marker = _read_json(config, f"{base}/complete/{name}")
        if not isinstance(marker, dict):
            raise SafetyError("completion marker is not a JSON object")
        validate_completion_marker(
            marker,
            backup_id=backup_id,
            prefix=prefix,
            timezone=timezone,
        )
        trusted = _authenticated_retention_record(
            config,
            remote=remote,
            prefix=prefix,
            backup_id=backup_id,
            identity=identity,
            timezone=timezone,
            manifest_verification_key=manifest_verification_key,
            manifest_authentication_key_id=manifest_authentication_key_id,
        )
        for field in ("frozen_at", "generations", "manifest_key", "object_keys"):
            if marker[field] != trusted[field]:
                raise SafetyError("completion marker conflicts with authenticated manifest")
        result[backup_id] = trusted
    return result


def _validated_journal_records(
    config: Path,
    *,
    remote: str,
    base: str,
    prefix: str,
    identity: Path,
    timezone: str,
    manifest_verification_key: bytes,
    manifest_authentication_key_id: str,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    records: dict[str, dict[str, Any]] = {}
    finalizing: set[str] = set()
    for name in _list_files(
        config,
        f"{base}/deleting",
        description="retention deletion journals",
    ):
        if not name.endswith(".json"):
            continue
        backup_id = name.removesuffix(".json")
        validate_backup_id(backup_id)
        manifest_target = f"{remote.rstrip('/')}/{prefix}/points/{backup_id}/manifest.json.age"
        if not _remote_file_exists(config, manifest_target):
            if _remote_file_exists(
                config, f"{base}/complete/{backup_id}.json"
            ) or _remote_file_exists(config, f"{base}/verified/{backup_id}.json"):
                raise SafetyError(
                    "deletion journal lost its manifest before control markers were removed"
                )
            finalizing.add(backup_id)
            continue
        trusted = _authenticated_retention_record(
            config,
            remote=remote,
            prefix=prefix,
            backup_id=backup_id,
            identity=identity,
            timezone=timezone,
            manifest_verification_key=manifest_verification_key,
            manifest_authentication_key_id=manifest_authentication_key_id,
        )
        journal = _read_json(config, f"{base}/deleting/{name}")
        if journal != trusted:
            raise SafetyError("retention deletion journal is not manifest-authenticated")
        records[backup_id] = trusted
    return records, finalizing


def _validated_verified_ids(
    config: Path,
    *,
    base: str,
    all_markers: dict[str, dict[str, Any]],
    restore_verification_key: bytes,
    restore_verification_key_id: str,
) -> set[str]:
    verified_ids: set[str] = set()
    for name in _list_files(
        config,
        f"{base}/verified",
        description="verified recovery points",
    ):
        if not name.endswith(".json"):
            continue
        backup_id = name.removesuffix(".json")
        validate_backup_id(backup_id)
        verification = _read_json(config, f"{base}/verified/{name}")
        if not isinstance(verification, dict):
            raise SafetyError("restore verification marker is not a JSON object")
        verify_restore_verification(
            verification,
            key=restore_verification_key,
            key_id=restore_verification_key_id,
        )
        marker = all_markers.get(backup_id)
        if (
            marker is None
            or verification["backup_id"] != backup_id
            or verification["manifest_sha256"] != marker["manifest_sha256"]
        ):
            raise SafetyError("restore verification marker is not bound to this recovery point")
        verified_ids.add(backup_id)
    return verified_ids


def rotate(
    *,
    config: Path,
    remote: str,
    prefix: str,
    identity: Path,
    manifest_verification_key_path: Path,
    manifest_authentication_key_id: str,
    restore_verification_key_path: Path,
    restore_verification_key_id: str,
    contract_path: Path,
    now: datetime,
    dry_run: bool,
) -> dict[str, Any]:
    """Compute references first and delete only expired, non-protected objects."""

    if os.environ.get("PR2A_DELETE_AUTHORIZED_ENVIRONMENT") != "1":
        raise SafetyError(
            "delete identity is allowed only in an independent authorized environment"
        )
    if not config.is_file() or config.stat().st_mode & 0o077:
        raise SafetyError("delete identity config must have mode 0600")
    try:
        identity_stat = identity.lstat()
    except OSError as error:
        raise SafetyError("offline age identity is missing") from error
    if (
        not identity.is_file()
        or identity.is_symlink()
        or identity_stat.st_mode & 0o077
        or identity_stat.st_uid != os.geteuid()
    ):
        raise SafetyError("offline age identity must be a regular mode-0600 operator-owned file")
    try:
        verification_key_stat = manifest_verification_key_path.lstat()
        manifest_verification_key = manifest_verification_key_path.read_bytes()
    except OSError as error:
        raise SafetyError("manifest verification key is missing") from error
    if (
        not manifest_verification_key_path.is_file()
        or manifest_verification_key_path.is_symlink()
        or verification_key_stat.st_mode & 0o077
        or verification_key_stat.st_uid != os.geteuid()
        or len(manifest_verification_key) != 32
    ):
        raise SafetyError(
            "manifest verification key must be a regular mode-0600 operator-owned 32-byte file"
        )
    try:
        restore_key_stat = restore_verification_key_path.lstat()
        restore_verification_key = restore_verification_key_path.read_bytes()
    except OSError as error:
        raise SafetyError("restore verification key is missing") from error
    if (
        not restore_verification_key_path.is_file()
        or restore_verification_key_path.is_symlink()
        or restore_key_stat.st_mode & 0o077
        or restore_key_stat.st_uid != os.geteuid()
        or len(restore_verification_key) != 32
    ):
        raise SafetyError(
            "restore verification key must be a regular mode-0600 operator-owned 32-byte file"
        )
    if ":" not in remote or remote.startswith("local:"):
        raise SafetyError("retention requires an S3-compatible rclone remote")
    contract = load_contract(contract_path)
    normalized_prefix = prefix.strip("/")
    base = f"{remote.rstrip('/')}/{normalized_prefix}"
    marker_by_id = _validated_completion_records(
        config,
        remote=remote,
        base=base,
        prefix=normalized_prefix,
        identity=identity,
        timezone=contract["business_timezone"],
        manifest_verification_key=manifest_verification_key,
        manifest_authentication_key_id=manifest_authentication_key_id,
    )
    journal_by_id, finalizing_journal_ids = _validated_journal_records(
        config,
        remote=remote,
        base=base,
        prefix=normalized_prefix,
        identity=identity,
        timezone=contract["business_timezone"],
        manifest_verification_key=manifest_verification_key,
        manifest_authentication_key_id=manifest_authentication_key_id,
    )
    all_markers = marker_by_id | journal_by_id
    verified_ids = _validated_verified_ids(
        config,
        base=base,
        all_markers=all_markers,
        restore_verification_key=restore_verification_key,
        restore_verification_key_id=restore_verification_key_id,
    )

    # Always protect the newest verified point. A deletion journal means some
    # referenced data may already be absent, so it cannot be republished as a
    # complete recovery point without authenticated per-object proof.
    protected_verified = (
        max(
            verified_ids,
            key=lambda backup_id: (
                str(all_markers[backup_id]["frozen_at"]),
                backup_id,
            ),
        )
        if verified_ids
        else None
    )
    if protected_verified is not None and protected_verified in journal_by_id:
        raise SafetyError(
            "protected verified point has a deletion journal; object integrity is unproven"
        )

    markers = [
        marker for backup_id, marker in marker_by_id.items() if backup_id not in journal_by_id
    ]
    decisions = retention_decisions(
        [
            {
                "backup_id": marker["backup_id"],
                "frozen_at": marker["frozen_at"],
                "generations": marker["generations"],
            }
            for marker in all_markers.values()
        ],
        now=now,
        timezone=contract["business_timezone"],
        unique_verified_backup_id=protected_verified,
    )
    for backup_id in journal_by_id:
        if decisions[backup_id] != "delete:expired_all":
            raise SafetyError("deletion journal no longer satisfies retention policy")
        decisions[backup_id] = "delete:resuming"
    for backup_id in finalizing_journal_ids:
        decisions[backup_id] = "delete:finalizing"
    kept = [marker for marker in markers if decisions[marker["backup_id"]].startswith("keep:")]
    deleted = [
        marker for marker in markers if decisions[marker["backup_id"]] == "delete:expired_all"
    ]
    referenced = {key for marker in kept for key in marker["object_keys"]}
    commands: list[str] = []
    deletion_records = dict(journal_by_id)
    for marker in deleted:
        backup_id = str(marker["backup_id"])
        deletion_records[backup_id] = marker
        if not dry_run and backup_id not in journal_by_id:
            _publish_and_verify_journal(
                config,
                target=f"{base}/deleting/{backup_id}.json",
                expected=marker,
            )

    for backup_id, marker in deletion_records.items():
        targets = [f"{base}/complete/{backup_id}.json"]
        manifest_key = str(marker["manifest_key"])
        targets.extend(
            f"{remote.rstrip('/')}/{key}"
            for key in marker["object_keys"]
            if key != manifest_key and key not in referenced
        )
        if backup_id in verified_ids and backup_id != protected_verified:
            targets.append(f"{base}/verified/{backup_id}.json")
        if manifest_key not in referenced:
            targets.append(f"{remote.rstrip('/')}/{manifest_key}")
        targets.append(f"{base}/deleting/{backup_id}.json")
        for target in targets:
            commands.append(target)
            if not dry_run:
                _delete_if_present(config, target)
    for backup_id in sorted(finalizing_journal_ids):
        target = f"{base}/deleting/{backup_id}.json"
        commands.append(target)
        if not dry_run:
            _delete_if_present(config, target)
    return {"decisions": decisions, "delete_targets": commands, "dry_run": dry_run}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--manifest-verification-key", type=Path, required=True)
    parser.add_argument("--manifest-authentication-key-id", required=True)
    parser.add_argument("--restore-verification-key", type=Path, required=True)
    parser.add_argument("--restore-verification-key-id", required=True)
    parser.add_argument("--contract", type=Path, default=Path("deploy/backup/contract.json"))
    parser.add_argument("--now", required=True)
    parser.add_argument("--apply", action="store_true")
    options = parser.parse_args()
    try:
        now = datetime.fromisoformat(options.now.replace("Z", "+00:00")).astimezone(UTC)
        result = rotate(
            config=options.config,
            remote=options.remote,
            prefix=options.prefix,
            identity=options.identity,
            manifest_verification_key_path=options.manifest_verification_key,
            manifest_authentication_key_id=options.manifest_authentication_key_id,
            restore_verification_key_path=options.restore_verification_key,
            restore_verification_key_id=options.restore_verification_key_id,
            contract_path=options.contract,
            now=now,
            dry_run=not options.apply,
        )
    except (SafetyError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"retention refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
