"""Content manifests, restore checkpoints, and retention decisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SAFE_BACKUP_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}$", re.ASCII)
SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
ED25519_SIGNATURE = re.compile(r"^[0-9a-f]{128}$", re.ASCII)
IMAGE_REFERENCE = re.compile(r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$", re.ASCII)
RECIPIENT_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", re.ASCII)
MANIFEST_AUTH_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", re.ASCII)
RESTORE_VERIFICATION_KEY_ID = MANIFEST_AUTH_KEY_ID


class SafetyError(RuntimeError):
    """An operation would violate a recovery or publication guard."""


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(tz=UTC)


def format_time(value: datetime) -> str:
    """Serialize timestamps in one stable UTC form."""

    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sha256_file(path: Path, *, chunk_bytes: int = 1024 * 1024) -> tuple[int, str]:
    """Hash file content, never metadata, and reject non-regular objects."""

    stat = path.lstat()
    if not path.is_file() or path.is_symlink():
        raise SafetyError(f"source object is not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_bytes):
            size += len(chunk)
            digest.update(chunk)
    if path.lstat().st_ino != stat.st_ino:
        raise SafetyError(f"source object changed identity while hashing: {path}")
    return size, digest.hexdigest()


def inventory(
    root: Path,
    *,
    excluded_top_level_names: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Build a complete deterministic source inventory by SHA-256 content."""

    if not root.is_absolute() or not root.is_dir():
        raise SafetyError("source root must be an existing absolute directory")
    excluded = frozenset(excluded_top_level_names)
    if any(not name or name in {".", ".."} or "/" in name or "\\" in name for name in excluded):
        raise SafetyError("invalid inventory exclusion")
    objects: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        relative_path = path.relative_to(root)
        if relative_path.parts and relative_path.parts[0] in excluded:
            continue
        relative = relative_path.as_posix()
        size, digest = sha256_file(path)
        objects.append({"path": relative, "size": size, "sha256": digest})
    return objects


def canonical_json(document: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes."""

    return (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def validate_backup_id(value: str) -> None:
    """Reject path-like or ambiguous restore-point identities."""

    if SAFE_BACKUP_ID.fullmatch(value) is None:
        raise SafetyError("invalid backup_id")


def _canonical_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise SafetyError(f"invalid completion {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SafetyError(f"invalid completion {field}") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or format_time(parsed) != value:
        raise SafetyError(f"non-canonical completion {field}")
    return parsed


def validate_completion_marker(
    marker: dict[str, Any],
    *,
    backup_id: str,
    prefix: str,
    timezone: str,
) -> None:
    """Validate every upload-controlled field before a delete identity uses it."""

    required = {
        "schema_version",
        "backup_id",
        "frozen_at",
        "verified_at",
        "manifest_key",
        "manifest_ciphertext_size",
        "manifest_ciphertext_sha256",
        "recipient_key_id",
        "generations",
        "object_keys",
        "status",
    }
    if set(marker) != required:
        raise SafetyError("completion marker schema mismatch")
    validate_backup_id(backup_id)
    if marker["schema_version"] != 1 or isinstance(marker["schema_version"], bool):
        raise SafetyError("unsupported completion marker schema")
    if marker["backup_id"] != backup_id:
        raise SafetyError("completion marker identity mismatch")

    frozen = _canonical_timestamp(marker["frozen_at"], field="frozen_at")
    verified = _canonical_timestamp(marker["verified_at"], field="verified_at")
    if verified <= frozen:
        raise SafetyError("completion verification time is not after freeze")
    generations = marker["generations"]
    expected_generations = sorted(generation_tags(frozen, timezone))
    if generations != expected_generations:
        raise SafetyError("completion generations mismatch")

    recipient_key_id = marker["recipient_key_id"]
    if (
        not isinstance(recipient_key_id, str)
        or RECIPIENT_KEY_ID.fullmatch(recipient_key_id) is None
    ):
        raise SafetyError("invalid completion recipient key id")
    if marker["status"] != "remote_ciphertext_verified":
        raise SafetyError("completion marker is not remotely verified")
    manifest_size = marker["manifest_ciphertext_size"]
    if not isinstance(manifest_size, int) or isinstance(manifest_size, bool) or manifest_size <= 0:
        raise SafetyError("invalid completion manifest size")
    if SHA256.fullmatch(str(marker["manifest_ciphertext_sha256"])) is None:
        raise SafetyError("invalid completion manifest digest")

    normalized_prefix = prefix.strip("/")
    if not normalized_prefix or ".." in Path(normalized_prefix).parts:
        raise SafetyError("invalid completion remote prefix")
    point_root = f"{normalized_prefix}/points/{backup_id}"
    expected_manifest_key = f"{point_root}/manifest.json.age"
    if marker["manifest_key"] != expected_manifest_key:
        raise SafetyError("completion manifest key mismatch")

    object_keys = marker["object_keys"]
    if (
        not isinstance(object_keys, list)
        or not object_keys
        or any(not isinstance(key, str) for key in object_keys)
        or object_keys != sorted(set(object_keys))
    ):
        raise SafetyError("completion object keys are not canonical")
    required_point_keys = {
        f"{point_root}/database.dump.age",
        f"{point_root}/config.tar.age",
        expected_manifest_key,
    }
    point_keys = {key for key in object_keys if key.startswith(f"{point_root}/")}
    if point_keys != required_point_keys:
        raise SafetyError("completion point object keys mismatch")

    cas_pattern = re.compile(
        rf"^{re.escape(normalized_prefix)}/objects/sha256/"
        rf"([0-9a-f]{{64}})/{re.escape(recipient_key_id)}\.(age|json)$",
        re.ASCII,
    )
    cas_suffixes: dict[str, set[str]] = {}
    for key in object_keys:
        if key in required_point_keys:
            continue
        match = cas_pattern.fullmatch(key)
        if match is None:
            raise SafetyError("completion object key outside allowed data namespace")
        cas_suffixes.setdefault(match.group(1), set()).add(match.group(2))
    if any(suffixes != {"age", "json"} for suffixes in cas_suffixes.values()):
        raise SafetyError("completion CAS object pair is incomplete")


def authenticate_manifest(
    manifest: dict[str, Any],
    *,
    key: bytes,
    key_id: str,
) -> dict[str, Any]:
    """Sign a canonical manifest with backup-only Ed25519 authority."""

    if len(key) != 32:
        raise SafetyError("manifest authentication key must be exactly 32 bytes")
    if MANIFEST_AUTH_KEY_ID.fullmatch(key_id) is None:
        raise SafetyError("invalid manifest authentication key id")
    if "authentication" in manifest:
        raise SafetyError("manifest is already authenticated")
    authenticated = dict(manifest)
    authenticated["authentication"] = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "signature": Ed25519PrivateKey.from_private_bytes(key).sign(canonical_json(manifest)).hex(),
    }
    return authenticated


def verify_manifest_authentication(
    manifest: dict[str, Any],
    *,
    key: bytes,
    key_id: str,
) -> None:
    """Verify sender authentication using a public key with no signing capability."""

    if len(key) != 32:
        raise SafetyError("manifest authentication key must be exactly 32 bytes")
    if MANIFEST_AUTH_KEY_ID.fullmatch(key_id) is None:
        raise SafetyError("invalid manifest authentication key id")
    authentication = manifest.get("authentication")
    if not isinstance(authentication, dict) or set(authentication) != {
        "algorithm",
        "key_id",
        "signature",
    }:
        raise SafetyError("manifest authentication schema mismatch")
    if authentication["algorithm"] != "ed25519" or authentication["key_id"] != key_id:
        raise SafetyError("manifest authentication authority mismatch")
    signature = authentication["signature"]
    if not isinstance(signature, str) or ED25519_SIGNATURE.fullmatch(signature) is None:
        raise SafetyError("invalid manifest authentication signature")
    unsigned = dict(manifest)
    del unsigned["authentication"]
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(
            bytes.fromhex(signature),
            canonical_json(unsigned),
        )
    except (InvalidSignature, ValueError) as error:
        raise SafetyError("manifest authentication failed") from error


def authenticate_restore_verification(
    verification: dict[str, Any],
    *,
    key: bytes,
    key_id: str,
) -> dict[str, Any]:
    """Sign a successful restore proof with restore-only authority."""

    _validate_restore_verification_unsigned(verification)
    if len(key) != 32:
        raise SafetyError("restore verification signing key must be exactly 32 bytes")
    if RESTORE_VERIFICATION_KEY_ID.fullmatch(key_id) is None:
        raise SafetyError("invalid restore verification key id")
    if "authentication" in verification:
        raise SafetyError("restore verification is already authenticated")
    authenticated = dict(verification)
    authenticated["authentication"] = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "signature": Ed25519PrivateKey.from_private_bytes(key)
        .sign(canonical_json(verification))
        .hex(),
    }
    return authenticated


def verify_restore_verification(
    verification: dict[str, Any],
    *,
    key: bytes,
    key_id: str,
) -> None:
    """Verify a restore proof using authority unavailable to backup/upload."""

    if len(key) != 32:
        raise SafetyError("restore verification key must be exactly 32 bytes")
    if RESTORE_VERIFICATION_KEY_ID.fullmatch(key_id) is None:
        raise SafetyError("invalid restore verification key id")
    authentication = verification.get("authentication")
    if not isinstance(authentication, dict) or set(authentication) != {
        "algorithm",
        "key_id",
        "signature",
    }:
        raise SafetyError("restore verification authentication schema mismatch")
    if authentication["algorithm"] != "ed25519" or authentication["key_id"] != key_id:
        raise SafetyError("restore verification authority mismatch")
    signature = authentication["signature"]
    if not isinstance(signature, str) or ED25519_SIGNATURE.fullmatch(signature) is None:
        raise SafetyError("invalid restore verification signature")
    unsigned = dict(verification)
    del unsigned["authentication"]
    _validate_restore_verification_unsigned(unsigned)
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(
            bytes.fromhex(signature),
            canonical_json(unsigned),
        )
    except (InvalidSignature, ValueError) as error:
        raise SafetyError("restore verification authentication failed") from error


def _validate_restore_verification_unsigned(verification: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "backup_id",
        "restore_operation_id",
        "environment_id",
        "verified_at",
        "rule",
        "restore_history_sha256",
        "manifest_sha256",
    }
    if set(verification) != required or verification.get("schema_version") != 1:
        raise SafetyError("restore verification schema mismatch")
    validate_backup_id(str(verification.get("backup_id", "")))
    if not str(verification.get("environment_id", "")).strip():
        raise SafetyError("restore verification environment is empty")
    _canonical_timestamp(verification.get("verified_at"), field="restore verified_at")
    if verification.get("rule") != "remote_download_plus_complete_isolated_restore_only":
        raise SafetyError("restore verification rule mismatch")
    for field in ("restore_operation_id", "restore_history_sha256", "manifest_sha256"):
        if SHA256.fullmatch(str(verification.get(field, ""))) is None:
            raise SafetyError(f"invalid restore verification {field}")


def retention_record_from_authenticated_manifest(
    manifest: dict[str, Any],
    *,
    backup_id: str,
    prefix: str,
    timezone: str,
    verification_key: bytes,
    authentication_key_id: str,
) -> dict[str, Any]:
    """Derive deletion keys only after sender authentication of the manifest."""

    verify_manifest_authentication(
        manifest,
        key=verification_key,
        key_id=authentication_key_id,
    )
    validate_manifest(manifest)
    if manifest["backup_id"] != backup_id:
        raise SafetyError("authenticated manifest identity mismatch")
    normalized_prefix = prefix.strip("/")
    if not normalized_prefix or ".." in Path(normalized_prefix).parts:
        raise SafetyError("invalid retention remote prefix")
    frozen = _canonical_timestamp(manifest["frozen_at"], field="manifest frozen_at")
    generations = sorted(generation_tags(frozen, timezone))
    if manifest.get("generations") != generations:
        raise SafetyError("authenticated manifest generations mismatch")
    recipient_key_id = manifest["recipient_key_id"]
    if (
        not isinstance(recipient_key_id, str)
        or RECIPIENT_KEY_ID.fullmatch(recipient_key_id) is None
    ):
        raise SafetyError("invalid authenticated recipient key id")

    point_root = f"{normalized_prefix}/points/{backup_id}"
    object_keys: set[str] = {
        f"{point_root}/manifest.json.age",
        f"{point_root}/database.dump.age",
        f"{point_root}/config.tar.age",
    }
    for item in manifest["objects"]:
        name = str(item["name"])
        key = str(item["key"])
        if name == "database":
            expected = f"{point_root}/database.dump.age"
        elif name == "config":
            expected = f"{point_root}/config.tar.age"
        elif name.startswith("file:"):
            digest = str(item["plaintext_sha256"])
            expected = f"{normalized_prefix}/objects/sha256/{digest}/{recipient_key_id}.age"
            object_keys.add(expected.removesuffix(".age") + ".json")
        else:
            raise SafetyError("unsupported authenticated manifest object")
        if key != expected:
            raise SafetyError("authenticated manifest object key mismatch")
        object_keys.add(key)
    return {
        "schema_version": 1,
        "backup_id": backup_id,
        "frozen_at": format_time(frozen),
        "generations": generations,
        "manifest_key": f"{point_root}/manifest.json.age",
        "object_keys": sorted(object_keys),
        "authentication_key_id": authentication_key_id,
        "manifest_sha256": hashlib.sha256(canonical_json(manifest)).hexdigest(),
        "source": "ed25519_authenticated_manifest",
    }


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Validate bundle identity and all object references before target access."""

    required = {
        "schema_version",
        "backup_id",
        "started_at",
        "frozen_at",
        "completed_at",
        "source_commit",
        "images",
        "config_hashes",
        "alembic_revision",
        "tools",
        "volume_name",
        "database_name",
        "recipient_key_id",
        "authentication",
        "objects",
        "files",
    }
    missing = required - manifest.keys()
    if missing:
        raise SafetyError(f"manifest fields missing: {','.join(sorted(missing))}")
    validate_backup_id(str(manifest["backup_id"]))
    if manifest["schema_version"] != 1:
        raise SafetyError("unsupported manifest schema")
    if len({manifest["started_at"], manifest["frozen_at"], manifest["completed_at"]}) != 3:
        raise SafetyError("backup timestamps are not distinct")
    for digest in manifest["config_hashes"].values():
        if SHA256.fullmatch(str(digest)) is None:
            raise SafetyError("invalid config digest")
    if not manifest["config_hashes"]:
        raise SafetyError("config identity is empty")
    if not manifest["images"] or any(
        IMAGE_REFERENCE.fullmatch(str(reference)) is None
        for reference in manifest["images"].values()
    ):
        raise SafetyError("invalid immutable image identity")
    if not str(manifest["recipient_key_id"]).strip():
        raise SafetyError("recipient key id is empty")
    authentication = manifest["authentication"]
    if (
        not isinstance(authentication, dict)
        or set(authentication) != {"algorithm", "key_id", "signature"}
        or authentication["algorithm"] != "ed25519"
        or MANIFEST_AUTH_KEY_ID.fullmatch(str(authentication["key_id"])) is None
        or ED25519_SIGNATURE.fullmatch(str(authentication["signature"])) is None
    ):
        raise SafetyError("invalid manifest authentication metadata")
    paths: set[str] = set()
    for item in manifest["files"]:
        path = str(item.get("path", ""))
        digest = str(item.get("sha256", ""))
        if not path or path.startswith("/") or ".." in Path(path).parts:
            raise SafetyError("unsafe file path in manifest")
        if path in paths:
            raise SafetyError("duplicate file path in manifest")
        paths.add(path)
        if SHA256.fullmatch(digest) is None or int(item.get("size", -1)) < 0:
            raise SafetyError("invalid file identity in manifest")
    object_names: set[str] = set()
    object_keys: set[str] = set()
    for item in manifest["objects"]:
        name = str(item.get("name", ""))
        key = str(item.get("key", ""))
        if not name or name in object_names or not key or key in object_keys:
            raise SafetyError("duplicate or empty object identity")
        object_names.add(name)
        object_keys.add(key)
        if key.startswith("/") or ".." in Path(key).parts:
            raise SafetyError("unsafe object key")
        for field in ("plaintext_sha256", "ciphertext_sha256"):
            if SHA256.fullmatch(str(item.get(field, ""))) is None:
                raise SafetyError(f"invalid {field}")
        for field in ("plaintext_size", "ciphertext_size"):
            if int(item.get(field, -1)) < 0:
                raise SafetyError(f"invalid {field}")
        if item.get("backup_id") != manifest["backup_id"]:
            raise SafetyError("mixed backup_id object")
    if {"database", "config"} - object_names:
        raise SafetyError("database or config object is missing")
    if {f"file:{path}" for path in paths} - object_names:
        raise SafetyError("file object is missing")


@dataclass(frozen=True)
class RestoreGuard:
    """Strongly bind an authorized destructive restore to one target and point."""

    environment_id: str
    backup_id: str
    operator_id: str
    approved_data_loss_window: str
    authorization_record: str
    expires_at: datetime
    challenge: str

    def validate(self, *, target_environment_id: str, supplied_challenge: str) -> None:
        """Fail before any destructive target write."""

        values = (
            self.environment_id,
            self.backup_id,
            self.operator_id,
            self.approved_data_loss_window,
            self.authorization_record,
            self.challenge,
            target_environment_id,
            supplied_challenge,
        )
        if any(not value.strip() for value in values):
            raise SafetyError("restore confirmation contains an empty value")
        validate_backup_id(self.backup_id)
        if self.environment_id != target_environment_id:
            raise SafetyError("restore environment marker mismatch")
        if not supplied_challenge or supplied_challenge != self.challenge:
            raise SafetyError("restore challenge mismatch")
        if utc_now() >= self.expires_at.astimezone(UTC):
            raise SafetyError("restore authorization expired")


def generation_tags(frozen_at: datetime, timezone: str) -> set[str]:
    """Assign deterministic daily/weekly/monthly generation membership."""

    local = frozen_at.astimezone(ZoneInfo(timezone))
    tags = {f"daily:{local.date().isoformat()}"}
    if local.weekday() == 0:
        year, week, _ = local.isocalendar()
        tags.add(f"weekly:{year}-W{week:02d}")
    if local.day == 1:
        tags.add(f"monthly:{local.year}-{local.month:02d}")
    return tags


def retention_decisions(
    points: Iterable[dict[str, Any]],
    *,
    now: datetime,
    timezone: str,
    unique_verified_backup_id: str | None,
) -> dict[str, str]:
    """Explain keep/delete decisions without ever deleting the only verified point."""

    local_now = now.astimezone(ZoneInfo(timezone))
    result: dict[str, str] = {}
    daily_cutoff = local_now - timedelta(days=14)
    weekly_cutoff = local_now - timedelta(weeks=8)
    month_index_now = local_now.year * 12 + local_now.month
    for point in points:
        backup_id = str(point["backup_id"])
        validate_backup_id(backup_id)
        frozen = datetime.fromisoformat(str(point["frozen_at"]).replace("Z", "+00:00"))
        local = frozen.astimezone(ZoneInfo(timezone))
        tags = set(point.get("generations") or generation_tags(frozen, timezone))
        reasons: list[str] = []
        if f"daily:{local.date().isoformat()}" in tags and local > daily_cutoff:
            reasons.append("daily")
        if any(tag.startswith("weekly:") for tag in tags) and local > weekly_cutoff:
            reasons.append("weekly")
        month_age = month_index_now - (local.year * 12 + local.month)
        if any(tag.startswith("monthly:") for tag in tags) and month_age < 6:
            reasons.append("monthly")
        if backup_id == unique_verified_backup_id:
            reasons.append("unique_verified")
        result[backup_id] = "keep:" + ",".join(reasons) if reasons else "delete:expired_all"
    return result


def atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Durably publish local non-sensitive state with a crash-safe replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
