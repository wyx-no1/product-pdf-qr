"""Immutable records and safety decisions for release rollback.

The module deliberately contains no database restore, migration downgrade, volume
cleanup, or secret-recovery primitive.  Destructive recovery is reachable only by
handing a validated, one-shot authorization to PR2A's unchanged restore entrypoint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RTO_LIMIT_SECONDS = 14_400
NEEDS_ROLLBACK_DECISION = 78
LOCK_BUSY = 75
SCHEMA_VERSION = 1

SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
GIT_SHA = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", re.ASCII)
BACKUP_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}$", re.ASCII)
IMAGE = re.compile(r"^[^@\s]+:[^@\s]+@sha256:([0-9a-f]{64})$", re.ASCII)

RUNNING_IMAGES = frozenset({"app", "migrate", "proxy", "db", "certbot", "pr2a"})
STAGES = ("prepared", "migrated", "isolated_validated", "public_cutover")
REQUIRED_COMPATIBILITY_ACTIONS = frozenset(
    {
        "login",
        "create",
        "import",
        "upload",
        "history_restore",
        "enable",
        "disable",
        "no_upload",
        "public_read",
        "audit_append",
        "constraints",
        "defaults",
        "enums",
        "triggers",
        "permissions",
    }
)
REQUIRED_RECOVERY_CONFIG = frozenset(
    {
        "compose.prod.yaml",
        "compose.backup.yaml",
        "deploy/production/nginx/nginx.conf",
        "deploy/production/nginx/templates/site.conf.template",
        "deploy/production/systemd/product-pdf-qr-backup-alert@.service",
        "deploy/production/systemd/product-pdf-qr-backup-finalize.service",
        "deploy/production/systemd/product-pdf-qr-backup-finalize.timer",
        "deploy/production/systemd/product-pdf-qr-backup-precopy.service",
        "deploy/production/systemd/product-pdf-qr-backup-precopy.timer",
        "deploy/production/systemd/product-pdf-qr-cert-renew.service",
        "deploy/production/systemd/product-pdf-qr-cert-renew.timer",
        "deploy/production/systemd/product-pdf-qr-log-rotate.service",
        "deploy/production/systemd/product-pdf-qr-log-rotate.timer",
        "deploy/backup/contract.json",
        "deploy/backup/requirements.txt",
        "scripts/backup_recovery/__init__.py",
        "scripts/backup_recovery/backup-run.sh",
        "scripts/backup_recovery/backup.py",
        "scripts/backup_recovery/cli.py",
        "scripts/backup_recovery/contract.py",
        "scripts/backup_recovery/crypto.py",
        "scripts/backup_recovery/emit-alert.sh",
        "scripts/backup_recovery/init-file-access.sh",
        "scripts/backup_recovery/lock.sh",
        "scripts/backup_recovery/model.py",
        "scripts/backup_recovery/rehearse-local.sh",
        "scripts/backup_recovery/remote.py",
        "scripts/backup_recovery/restore-run.sh",
        "scripts/backup_recovery/restore.py",
        "scripts/backup_recovery/synthetic_functional_check.py",
        "scripts/backup_recovery/validate_compose.py",
        "scripts/backup_recovery/verify-reproducible-image.sh",
    }
)
ALLOWED_APP_CONFIG = frozenset(
    {
        "app-runtime.json",
        "app-limits.json",
        "app-public-origin.json",
    }
)
ALLOWED_APP_ENVIRONMENT = frozenset(
    {
        "APP_PORT",
        "DB_POOL_MIN_SIZE",
        "DB_POOL_MAX_SIZE",
        "STORAGE_ROOT",
        "PUBLIC_DOMAIN",
        "PUBLIC_BASE_URL",
        "MAX_PDF_BYTES",
        "IMPORT_MAX_UPLOAD_BYTES",
        "IMPORT_MAX_DECOMPRESSED_BYTES",
        "IMPORT_MAX_COMPRESSION_RATIO",
        "IMPORT_PARSE_TIMEOUT_SECONDS",
        "IMPORT_PARSE_MEMORY_BYTES",
        "IMPORT_MAX_ROWS",
        "PDF_VALIDATION_TIMEOUT_SECONDS",
        "PDF_VALIDATION_CPU_SECONDS",
        "PDF_VALIDATION_MEMORY_BYTES",
        "PUBLIC_MISS_LIMIT",
        "PUBLIC_MISS_WINDOW_SECONDS",
        "SESSION_COOKIE_SECURE",
        "SESSION_TTL_SECONDS",
        "LOGIN_FAILURE_LIMIT",
        "LOGIN_FAILURE_WINDOW_SECONDS",
        "LOGIN_BACKOFF_BASE_SECONDS",
        "LOGIN_BACKOFF_MAX_SECONDS",
    }
)
FORBIDDEN_CONFIG_WORDS = (
    "age-secret-key-",
    "-----begin private key-----",
    "aws_secret_access_key =",
)
SENSITIVE_AUDIT_WORDS = (
    "password",
    "secret",
    "private_key",
    "credential",
    "token",
    "pdf_content",
    "challenge",
)


class RollbackSafetyError(RuntimeError):
    """A release or rollback action is unsafe or unverifiable."""


class RollbackDecisionRequired(RollbackSafetyError):
    """Automation must stop at a non-zero human decision node."""


def canonical_json(value: Any) -> bytes:
    """Encode deterministic JSON used by every digest and persistent record."""

    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def digest_json(value: Any) -> str:
    """Return the SHA-256 identity of a canonical JSON value."""

    return hashlib.sha256(canonical_json(value)).hexdigest()


def format_time(value: datetime) -> str:
    """Use a single canonical UTC timestamp representation."""

    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(value: Any, *, field: str) -> datetime:
    """Parse and require the canonical UTC timestamp representation."""

    if not isinstance(value, str):
        raise RollbackSafetyError(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RollbackSafetyError(f"{field} must be a canonical UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise RollbackSafetyError(f"{field} must be a canonical UTC timestamp")
    if format_time(parsed) != value:
        raise RollbackSafetyError(f"{field} must be a canonical UTC timestamp")
    return parsed.astimezone(UTC)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Persist a complete file with file and parent-directory fsync."""

    if not path.is_absolute():
        raise RollbackSafetyError("persistent path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_create(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Create an immutable record, refusing same-name overwrite and replay."""

    if not path.is_absolute():
        raise RollbackSafetyError("persistent path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError as error:
        raise RollbackSafetyError("immutable record already exists") from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def read_json_file(path: Path) -> dict[str, Any]:
    """Read a regular, non-symlink JSON object."""

    try:
        metadata = path.lstat()
        if not path.is_file() or path.is_symlink() or metadata.st_mode & 0o022:
            raise RollbackSafetyError(f"unsafe persistent file: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RollbackSafetyError(f"invalid persistent JSON: {path}") from error
    if not isinstance(value, dict):
        raise RollbackSafetyError(f"persistent JSON is not an object: {path}")
    return value


def _required_text(document: Mapping[str, Any], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RollbackSafetyError(f"{field} is required")
    return value


def _safe_id(document: Mapping[str, Any], field: str) -> str:
    value = _required_text(document, field)
    if SAFE_ID.fullmatch(value) is None:
        raise RollbackSafetyError(f"{field} is invalid")
    return value


def _exact_image(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or IMAGE.fullmatch(value) is None:
        raise RollbackSafetyError(f"{field} must be an exact tag@sha256 image")
    return value


def _image_digest(value: str) -> str:
    match = IMAGE.fullmatch(value)
    if match is None:
        raise RollbackSafetyError("image reference is not exact")
    return match.group(1)


def _validate_config_artifact(
    name: str,
    artifact: Any,
    *,
    rollback_window_ends_at: datetime,
    allowlist: frozenset[str] = ALLOWED_APP_CONFIG,
) -> None:
    if name not in allowlist or not isinstance(artifact, dict):
        raise RollbackSafetyError("non-secret config is outside its component allowlist")
    if set(artifact) != {"content_b64", "sha256", "retained_until"}:
        raise RollbackSafetyError("config artifact schema mismatch")
    content = artifact["content_b64"]
    if not isinstance(content, str) or not content:
        raise RollbackSafetyError("config artifact body is required")
    try:
        decoded = base64.b64decode(content, validate=True)
    except ValueError as error:
        raise RollbackSafetyError("config artifact body is not canonical base64") from error
    if base64.b64encode(decoded).decode() != content:
        raise RollbackSafetyError("config artifact body is not canonical base64")
    digest = artifact["sha256"]
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise RollbackSafetyError("config artifact digest is invalid")
    if hashlib.sha256(decoded).hexdigest() != digest:
        raise RollbackSafetyError("config artifact digest mismatch")
    lowered = decoded.decode("utf-8", errors="ignore").lower()
    if any(word in lowered for word in FORBIDDEN_CONFIG_WORDS):
        raise RollbackSafetyError("config artifact appears to contain a secret")
    if re.search(r"postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:[^@\s]+@", lowered):
        raise RollbackSafetyError("config artifact appears to contain database credentials")
    if name == "app-runtime.json":
        try:
            runtime = json.loads(decoded)
        except json.JSONDecodeError as error:
            raise RollbackSafetyError("app runtime config must be JSON") from error
        if (
            not isinstance(runtime, dict)
            or not runtime
            or set(runtime) - ALLOWED_APP_ENVIRONMENT
            or any(not isinstance(value, str) or not value for value in runtime.values())
        ):
            raise RollbackSafetyError("app runtime config contains a forbidden environment field")
    retained_until = parse_time(artifact["retained_until"], field="config.retained_until")
    if retained_until < rollback_window_ends_at:
        raise RollbackSafetyError("config artifact retention is shorter than rollback window")


def _validate_artifact_set(
    artifact_set: Any,
    *,
    name: str,
    rollback_window_ends_at: datetime,
) -> None:
    if not isinstance(artifact_set, dict):
        raise RollbackSafetyError(f"{name} artifact set is missing")
    if set(artifact_set) != {
        "commit",
        "alembic_revision",
        "migration_sha",
        "images",
        "image_evidence",
        "recovery_config",
        "app_config",
        "secret_references",
    }:
        raise RollbackSafetyError(f"{name} artifact set schema mismatch")
    if GIT_SHA.fullmatch(_required_text(artifact_set, "commit")) is None:
        raise RollbackSafetyError(f"{name} commit must be an exact Git SHA")
    _safe_id(artifact_set, "alembic_revision")
    if GIT_SHA.fullmatch(_required_text(artifact_set, "migration_sha")) is None:
        raise RollbackSafetyError(f"{name} migration_sha must be an exact Git SHA")

    images = artifact_set["images"]
    evidence = artifact_set["image_evidence"]
    if not isinstance(images, dict) or set(images) != RUNNING_IMAGES:
        raise RollbackSafetyError(f"{name} must bind every running image")
    if not isinstance(evidence, dict) or set(evidence) != RUNNING_IMAGES:
        raise RollbackSafetyError(f"{name} image evidence is incomplete")
    for component in sorted(RUNNING_IMAGES):
        reference = _exact_image(images[component], field=f"{name}.images.{component}")
        proof = evidence[component]
        if not isinstance(proof, dict) or set(proof) != {
            "registry_digest",
            "image_id_digest",
            "prefetched",
            "retained_until",
        }:
            raise RollbackSafetyError(f"{name}.{component} image evidence schema mismatch")
        expected = _image_digest(reference)
        if (
            proof["registry_digest"] != expected
            or not isinstance(proof["image_id_digest"], str)
            or SHA256.fullmatch(proof["image_id_digest"]) is None
        ):
            raise RollbackSafetyError(f"{name}.{component} fetched image identity mismatch")
        if proof["prefetched"] is not True:
            raise RollbackSafetyError(f"{name}.{component} image was not prefetched")
        retained_until = parse_time(
            proof["retained_until"],
            field=f"{name}.{component}.retained_until",
        )
        if retained_until < rollback_window_ends_at:
            raise RollbackSafetyError(f"{name}.{component} retention is too short")

    for config_field, allowlist in (
        ("recovery_config", frozenset()),
        ("app_config", ALLOWED_APP_CONFIG),
    ):
        configs = artifact_set[config_field]
        if not isinstance(configs, dict) or not configs:
            raise RollbackSafetyError(f"{name} retrievable {config_field} is required")
        if config_field == "recovery_config" and not REQUIRED_RECOVERY_CONFIG <= set(configs):
            raise RollbackSafetyError(f"{name} recovery config archive is incomplete")
        for config_name, artifact in configs.items():
            if not isinstance(config_name, str):
                raise RollbackSafetyError("config name is invalid")
            if config_field == "recovery_config":
                allowed = (
                    config_name in {"compose.prod.yaml", "compose.backup.yaml"}
                    or config_name.startswith("deploy/production/nginx/")
                    or config_name.startswith("deploy/production/systemd/")
                    or config_name.startswith("deploy/backup/")
                    or config_name.startswith("scripts/backup_recovery/")
                )
                if not allowed or config_name.startswith("/") or ".." in Path(config_name).parts:
                    raise RollbackSafetyError("recovery config is outside the PR2A allowlist")
            _validate_config_artifact(
                config_name,
                artifact,
                rollback_window_ends_at=rollback_window_ends_at,
                allowlist=allowlist if config_field == "app_config" else frozenset({config_name}),
            )

    secret_references = artifact_set["secret_references"]
    if not isinstance(secret_references, dict) or not secret_references:
        raise RollbackSafetyError(f"{name} secret references are required")
    for key, reference in secret_references.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(reference, str)
            or not reference.strip()
        ):
            raise RollbackSafetyError("secret references must be versioned non-empty strings")
        if "\n" in reference or "=" in reference:
            raise RollbackSafetyError("secret reference may not contain a secret value")


def release_identity(record: Mapping[str, Any]) -> str:
    """Bind G-19 and migration responsibility to every relevant exact artifact."""

    stable = record.get("stable")
    candidate = record.get("candidate")
    if not isinstance(stable, dict) or not isinstance(candidate, dict):
        raise RollbackSafetyError("release artifacts are missing")

    def identity(artifacts: Mapping[str, Any]) -> dict[str, Any]:
        configs = artifacts.get("app_config")
        if not isinstance(configs, dict):
            raise RollbackSafetyError("release config identity is missing")
        recovery_configs = artifacts.get("recovery_config")
        if not isinstance(recovery_configs, dict):
            raise RollbackSafetyError("release recovery config identity is missing")
        return {
            "commit": artifacts.get("commit"),
            "alembic_revision": artifacts.get("alembic_revision"),
            "migration_sha": artifacts.get("migration_sha"),
            "images": artifacts.get("images"),
            "recovery_config_sha256": {
                name: item.get("sha256") if isinstance(item, dict) else None
                for name, item in sorted(recovery_configs.items())
            },
            "config_sha256": {
                name: item.get("sha256") if isinstance(item, dict) else None
                for name, item in sorted(configs.items())
            },
        }

    return digest_json(
        {
            "release_id": record.get("release_id"),
            "environment_id": record.get("environment_id"),
            "stable": identity(stable),
            "candidate": identity(candidate),
            "pre_release_backup": (
                {
                    field: record["pre_release_backup"].get(field)
                    for field in (
                        "backup_id",
                        "source_commit",
                        "images",
                        "config_sha256",
                        "alembic_revision",
                        "completed_at",
                        "g19_watermark_sha256",
                    )
                }
                if isinstance(record.get("pre_release_backup"), dict)
                else None
            ),
            "publication_fence": (
                {
                    "command_sha256": record["publication_fence"].get("command_sha256"),
                    "readiness_bypass_only": record["publication_fence"].get(
                        "readiness_bypass_only"
                    ),
                }
                if isinstance(record.get("publication_fence"), dict)
                else None
            ),
        }
    )


def _validate_approval(value: Any, *, field: str) -> None:
    if not isinstance(value, dict) or set(value) != {"approval_id", "approver", "approved_at"}:
        raise RollbackSafetyError(f"{field} approval schema mismatch")
    _safe_id(value, "approval_id")
    _safe_id(value, "approver")
    parse_time(value["approved_at"], field=f"{field}.approved_at")


def _validate_backup(record: Mapping[str, Any], backup: Any, *, declared_at: datetime) -> None:
    if not isinstance(backup, dict) or set(backup) != {
        "backup_id",
        "source_commit",
        "images",
        "config_sha256",
        "alembic_revision",
        "frozen_at",
        "completed_at",
        "encrypted",
        "manifest_authenticated",
        "completion_last",
        "remote_verified",
        "preflight_retrievable",
        "g19_watermark_sha256",
    }:
        raise RollbackSafetyError("pre-release backup schema mismatch")
    backup_id = _required_text(backup, "backup_id")
    if BACKUP_ID.fullmatch(backup_id) is None:
        raise RollbackSafetyError("pre-release backup_id is invalid")
    frozen = parse_time(backup["frozen_at"], field="backup.frozen_at")
    completed = parse_time(backup["completed_at"], field="backup.completed_at")
    if completed <= frozen or completed > declared_at:
        raise RollbackSafetyError("pre-release backup timing is invalid")
    if (declared_at - completed).total_seconds() > 3600:
        raise RollbackSafetyError("pre-release backup is not immediate")
    for field in (
        "encrypted",
        "manifest_authenticated",
        "completion_last",
        "remote_verified",
        "preflight_retrievable",
    ):
        if backup[field] is not True:
            raise RollbackSafetyError(f"pre-release backup {field} proof is missing")
    watermark_sha256 = backup["g19_watermark_sha256"]
    if not isinstance(watermark_sha256, str) or SHA256.fullmatch(watermark_sha256) is None:
        raise RollbackSafetyError("pre-release backup G-19 watermark is missing")
    stable = record["stable"]
    if backup["source_commit"] != stable["commit"]:
        raise RollbackSafetyError("pre-release backup source commit mismatch")
    if backup["images"] != stable["images"]:
        raise RollbackSafetyError("pre-release backup image identity mismatch")
    expected_configs = {
        name: artifact["sha256"] for name, artifact in sorted(stable["recovery_config"].items())
    }
    if backup["config_sha256"] != expected_configs:
        raise RollbackSafetyError("pre-release backup config identity mismatch")
    if backup["alembic_revision"] != stable["alembic_revision"]:
        raise RollbackSafetyError("pre-release backup Alembic identity mismatch")


def validate_release_record(record: Mapping[str, Any]) -> str:
    """Validate a complete immutable record and return its release identity."""

    if set(record) != {
        "schema_version",
        "release_id",
        "environment_id",
        "declared_at",
        "rollback_window_ends_at",
        "stable",
        "candidate",
        "pre_release_backup",
        "compatibility",
        "release_approval",
        "publication_fence",
        "stable_isolation_smoke",
        "pre_publication_plan",
    }:
        raise RollbackSafetyError("release record schema mismatch")
    if record["schema_version"] != SCHEMA_VERSION:
        raise RollbackSafetyError("unsupported release record schema")
    _safe_id(record, "release_id")
    _safe_id(record, "environment_id")
    declared_at = parse_time(record["declared_at"], field="declared_at")
    rollback_window = parse_time(
        record["rollback_window_ends_at"],
        field="rollback_window_ends_at",
    )
    if rollback_window <= declared_at:
        raise RollbackSafetyError("rollback window must end after release declaration")
    _validate_artifact_set(
        record["stable"],
        name="stable",
        rollback_window_ends_at=rollback_window,
    )
    _validate_artifact_set(
        record["candidate"],
        name="candidate",
        rollback_window_ends_at=rollback_window,
    )
    if record["stable"]["commit"] == record["candidate"]["commit"]:
        raise RollbackSafetyError("stable and candidate commits must differ")
    _validate_backup(record, record["pre_release_backup"], declared_at=declared_at)
    _validate_approval(record["release_approval"], field="release")
    fence = record["publication_fence"]
    if (
        not isinstance(fence, dict)
        or set(fence) != {"command_sha256", "readiness_bypass_only", "approval"}
        or not isinstance(fence["command_sha256"], str)
        or SHA256.fullmatch(fence["command_sha256"]) is None
        or fence["readiness_bypass_only"] is not True
    ):
        raise RollbackSafetyError("approved publication fence is incomplete")
    _validate_approval(fence["approval"], field="publication_fence")

    smoke = record["stable_isolation_smoke"]
    if (
        not isinstance(smoke, dict)
        or set(smoke) != {"passed", "run_id", "release_identity"}
        or smoke["passed"] is not True
    ):
        raise RollbackSafetyError("stable isolated smoke evidence is incomplete")
    _safe_id(smoke, "run_id")

    compatibility = record["compatibility"]
    if not isinstance(compatibility, dict) or set(compatibility) != {
        "verdict",
        "migration_owner",
        "decided_at",
        "release_identity",
        "approval",
        "full_read_write_actions",
        "g19_rehearsal",
    }:
        raise RollbackSafetyError("compatibility record schema mismatch")
    if compatibility["verdict"] not in {"compatible", "incompatible"}:
        raise RollbackSafetyError("compatibility verdict is missing or unknown")
    _safe_id(compatibility, "migration_owner")
    parse_time(compatibility["decided_at"], field="compatibility.decided_at")
    _validate_approval(compatibility["approval"], field="compatibility")
    actions = compatibility["full_read_write_actions"]
    if (
        not isinstance(actions, dict)
        or set(actions) != REQUIRED_COMPATIBILITY_ACTIONS
        or any(not isinstance(value, bool) for value in actions.values())
    ):
        raise RollbackSafetyError("complete old-app read/write compatibility evidence is required")
    if compatibility["verdict"] == "compatible" and any(
        value is not True for value in actions.values()
    ):
        raise RollbackSafetyError("compatible requires every old-app read/write action")

    rehearsal = compatibility["g19_rehearsal"]
    if (
        not isinstance(rehearsal, dict)
        or set(rehearsal) != {"passed", "run_id", "release_identity"}
        or rehearsal["passed"] is not True
    ):
        raise RollbackSafetyError("release-specific G-19 rehearsal is required")
    _safe_id(rehearsal, "run_id")

    identity = release_identity(record)
    for observed in (
        smoke["release_identity"],
        compatibility["release_identity"],
        rehearsal["release_identity"],
    ):
        if observed != identity:
            raise RollbackSafetyError("exact release evidence is stale or cross-bound")

    plan = record["pre_publication_plan"]
    if not isinstance(plan, dict) or set(plan) != {"roll_forward", "lossy_recovery"}:
        raise RollbackSafetyError("pre-publication plan schema mismatch")
    if compatibility["verdict"] == "incompatible":
        roll_forward = plan["roll_forward"]
        lossy = plan["lossy_recovery"]
        roll_forward_valid = (
            isinstance(roll_forward, dict)
            and set(roll_forward) == {"rehearsed", "run_id", "release_identity", "approval"}
            and roll_forward["rehearsed"] is True
            and roll_forward["release_identity"] == identity
        )
        lossy_valid = (
            isinstance(lossy, dict)
            and set(lossy)
            == {
                "preapproved",
                "authorization_record",
                "loss_start",
                "loss_end",
                "release_identity",
                "approval",
            }
            and lossy["preapproved"] is True
            and lossy["release_identity"] == identity
        )
        if roll_forward_valid:
            _safe_id(roll_forward, "run_id")
            _validate_approval(roll_forward["approval"], field="roll_forward")
        if lossy_valid:
            _safe_id(lossy, "authorization_record")
            loss_start = parse_time(lossy["loss_start"], field="lossy.loss_start")
            loss_end = parse_time(lossy["loss_end"], field="lossy.loss_end")
            if loss_end <= loss_start:
                raise RollbackSafetyError("approved data-loss window is invalid")
            _validate_approval(lossy["approval"], field="lossy")
        if not (roll_forward_valid or lossy_valid):
            raise RollbackSafetyError(
                "incompatible release lacks rehearsed roll-forward or approved data plan"
            )
    return identity


class ReleaseStore:
    """Append-only immutable release records with digest sidecars."""

    def __init__(self, root: Path):
        if not root.is_absolute() or root == Path("/"):
            raise RollbackSafetyError("release store root must be a bounded absolute path")
        self.root = root

    def seal(self, record: Mapping[str, Any]) -> str:
        identity = validate_release_record(record)
        release_id = str(record["release_id"])
        path = self.root / f"{release_id}.json"
        payload = canonical_json(record)
        digest = hashlib.sha256(payload).hexdigest()
        atomic_create(path, payload)
        try:
            atomic_create(path.with_suffix(".sha256"), f"{digest}\n".encode())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return identity

    def load(self, release_id: str) -> dict[str, Any]:
        if SAFE_ID.fullmatch(release_id) is None:
            raise RollbackSafetyError("release_id is invalid")
        path = self.root / f"{release_id}.json"
        record = read_json_file(path)
        expected_path = path.with_suffix(".sha256")
        try:
            metadata = expected_path.lstat()
            if (
                not expected_path.is_file()
                or expected_path.is_symlink()
                or metadata.st_mode & 0o022
            ):
                raise RollbackSafetyError("release record digest file is unsafe")
            expected = expected_path.read_text(encoding="ascii").strip()
        except OSError as error:
            raise RollbackSafetyError("release record digest is missing") from error
        actual = hashlib.sha256(canonical_json(record)).hexdigest()
        if SHA256.fullmatch(expected) is None or actual != expected:
            raise RollbackSafetyError("immutable release record digest mismatch")
        validate_release_record(record)
        return record


def validate_watermark(watermark: Any) -> str:
    """Validate relation, complete file, and audit projections."""

    if not isinstance(watermark, dict) or set(watermark) != {
        "relations",
        "files",
        "audit_projection",
    }:
        raise RollbackSafetyError("full data watermark schema mismatch")
    relations = watermark["relations"]
    required_relations = {
        "admins",
        "products",
        "pdf_files",
        "pdf_versions",
        "admin_sessions",
        "audit_events",
    }
    if (
        not isinstance(relations, dict)
        or set(relations) != required_relations
        or any(
            not isinstance(value, str) or SHA256.fullmatch(value) is None
            for value in relations.values()
        )
    ):
        raise RollbackSafetyError("all stable relation projections are required")
    files = watermark["files"]
    if not isinstance(files, list):
        raise RollbackSafetyError("complete historical file manifest is required")
    paths: list[str] = []
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size", "sha256"}
            or not isinstance(item["path"], str)
            or not item["path"]
            or item["path"].startswith(("/", "../"))
            or ".." in Path(item["path"]).parts
            or not isinstance(item["size"], int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
            or not isinstance(item["sha256"], str)
            or SHA256.fullmatch(item["sha256"]) is None
        ):
            raise RollbackSafetyError("historical file manifest item is invalid")
        paths.append(item["path"])
    if paths != sorted(set(paths)):
        raise RollbackSafetyError("historical file manifest must be complete and canonical")
    audit = watermark["audit_projection"]
    if not isinstance(audit, str) or SHA256.fullmatch(audit) is None:
        raise RollbackSafetyError("audit projection digest is required")
    return digest_json(watermark)


def validate_execution_environment(
    marker_path: Path,
    *,
    record: Mapping[str, Any],
    operation_id: str,
    operator: str,
    confirmation: str,
) -> dict[str, Any]:
    """Reject empty/default/mixed targets before secrets or service controls are reached."""

    marker = read_json_file(marker_path)
    if (
        set(marker)
        != {
            "schema_version",
            "kind",
            "environment_id",
            "docker_context",
            "compose_project",
            "resource_prefix",
            "target_marker",
            "publication_fence_command",
        }
        or marker.get("schema_version") != SCHEMA_VERSION
    ):
        raise RollbackSafetyError("execution environment marker schema mismatch")
    environment_id = _safe_id(marker, "environment_id")
    docker_context = _safe_id(marker, "docker_context")
    compose_project = _safe_id(marker, "compose_project")
    resource_prefix = _safe_id(marker, "resource_prefix")
    fence_command = marker["publication_fence_command"]
    if (
        not isinstance(fence_command, list)
        or not fence_command
        or any(not isinstance(item, str) or not item for item in fence_command)
        or digest_json(fence_command) != record["publication_fence"]["command_sha256"]
    ):
        raise RollbackSafetyError("publication fence command differs from release approval")
    kind = marker["kind"]
    if (
        environment_id != record.get("environment_id")
        or docker_context in {"default", "desktop-linux"}
        or kind not in {"synthetic", "production"}
    ):
        raise RollbackSafetyError("execution environment identity mismatch or default context")
    if kind == "synthetic":
        if (
            not environment_id.startswith("synthetic-")
            or not docker_context.startswith("synthetic-")
            or not compose_project.startswith("synthetic-")
            or not resource_prefix.startswith("synthetic-")
            or marker["target_marker"] != "SYNTHETIC_PR2B_LOCAL_ONLY"
        ):
            raise RollbackSafetyError("synthetic/production environment confusion")
        expected_confirmation = f"synthetic:{environment_id}:{operation_id}"
    else:
        if (
            not environment_id.startswith("production-")
            or marker["target_marker"] != "AUTHORIZED_PRODUCTION"
        ):
            raise RollbackSafetyError("production environment marker is invalid")
        expected_confirmation = (
            f"production:{record.get('release_id')}:{operation_id}:{environment_id}:{operator}"
        )
    if confirmation != expected_confirmation:
        raise RollbackSafetyError("operation-specific environment confirmation mismatch")
    return marker


class PublicationState:
    """Persistent monotonic publication state with cutover-before-publication guard."""

    def __init__(self, path: Path, *, release_id: str, environment_id: str):
        if not path.is_absolute() or path == Path("/"):
            raise RollbackSafetyError("publication state path must be bounded and absolute")
        if SAFE_ID.fullmatch(release_id) is None or SAFE_ID.fullmatch(environment_id) is None:
            raise RollbackSafetyError("publication identity is invalid")
        self.path = path
        self.integrity_path = path.with_suffix(f"{path.suffix}.sha256")
        self.cutover_marker = path.with_suffix(f"{path.suffix}.public-cutover")
        self.release_id = release_id
        self.environment_id = environment_id

    def prepare(self, watermark: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
        if self.cutover_marker.exists():
            raise RollbackSafetyError("previous public cutover marker forbids state reuse")
        watermark_digest = validate_watermark(watermark)
        state = {
            "schema_version": SCHEMA_VERSION,
            "release_id": self.release_id,
            "environment_id": self.environment_id,
            "stage": "prepared",
            "proxy_ever_public": False,
            "baseline_watermark": dict(watermark),
            "baseline_watermark_digest": watermark_digest,
            "history": [{"stage": "prepared", "at": format_time(now)}],
        }
        payload = canonical_json(state)
        atomic_create(self.path, payload)
        try:
            atomic_create(
                self.integrity_path,
                f"{hashlib.sha256(payload).hexdigest()}\n".encode(),
            )
        except BaseException:
            self.path.unlink(missing_ok=True)
            raise
        return state

    def read(self) -> dict[str, Any]:
        state = read_json_file(self.path)
        try:
            expected = self.integrity_path.read_text(encoding="ascii").strip()
        except OSError as error:
            raise RollbackSafetyError("publication state integrity seal is missing") from error
        if (
            self.integrity_path.is_symlink()
            or not self.integrity_path.is_file()
            or SHA256.fullmatch(expected) is None
            or hashlib.sha256(canonical_json(state)).hexdigest() != expected
        ):
            raise RollbackSafetyError("publication state integrity seal mismatch")
        if (
            set(state)
            != {
                "schema_version",
                "release_id",
                "environment_id",
                "stage",
                "proxy_ever_public",
                "baseline_watermark",
                "baseline_watermark_digest",
                "history",
            }
            or state["schema_version"] != SCHEMA_VERSION
            or state["release_id"] != self.release_id
            or state["environment_id"] != self.environment_id
            or state["stage"] not in STAGES
            or not isinstance(state["proxy_ever_public"], bool)
            or validate_watermark(state["baseline_watermark"]) != state["baseline_watermark_digest"]
            or not isinstance(state["history"], list)
            or not state["history"]
        ):
            raise RollbackSafetyError("publication state is missing, corrupt, or unknown")
        history_stages = [
            item.get("stage") if isinstance(item, dict) else None for item in state["history"]
        ]
        expected_history = list(STAGES[: STAGES.index(str(state["stage"])) + 1])
        if history_stages != expected_history:
            raise RollbackSafetyError("publication state history is non-monotonic")
        if state["proxy_ever_public"] != (state["stage"] == "public_cutover"):
            raise RollbackSafetyError("publication proxy/cutover evidence conflicts")
        if self.cutover_marker.exists():
            marker = read_json_file(self.cutover_marker)
            if (
                marker.get("schema_version") != SCHEMA_VERSION
                or marker.get("release_id") != self.release_id
                or marker.get("environment_id") != self.environment_id
                or state["stage"] != "public_cutover"
            ):
                raise RollbackSafetyError("irreversible public cutover marker conflicts")
        elif state["stage"] == "public_cutover":
            raise RollbackSafetyError("irreversible public cutover marker is missing")
        return state

    def advance(self, stage: str, *, now: datetime) -> dict[str, Any]:
        if stage not in STAGES:
            raise RollbackSafetyError("unknown publication stage")
        state = self.read()
        current = STAGES.index(str(state["stage"]))
        requested = STAGES.index(stage)
        if requested != current + 1:
            raise RollbackSafetyError("publication state must advance exactly one stage")
        state["stage"] = stage
        if stage == "public_cutover":
            # This durable marker is written before a proxy/DNS operation is permitted.
            atomic_create(
                self.cutover_marker,
                canonical_json(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "release_id": self.release_id,
                        "environment_id": self.environment_id,
                        "cutover_at": format_time(now),
                    }
                ),
            )
            state["proxy_ever_public"] = True
        state["history"].append({"stage": stage, "at": format_time(now)})
        payload = canonical_json(state)
        atomic_write(self.path, payload)
        atomic_write(
            self.integrity_path,
            f"{hashlib.sha256(payload).hexdigest()}\n".encode(),
        )
        return state

    def authorize_proxy_start(self) -> None:
        state = self.read()
        if state["stage"] != "public_cutover" or state["proxy_ever_public"] is not True:
            raise RollbackSafetyError("proxy remains blocked until durable public_cutover")


def choose_rollback_path(
    state_loader: Callable[[], Mapping[str, Any]],
    current_watermark: Mapping[str, Any],
    *,
    proxy_continuously_isolated: bool,
) -> str:
    """Use path one only with positive proof of all three pre-public conditions."""

    validate_watermark(current_watermark)
    try:
        state = state_loader()
        stage = state.get("stage")
        baseline = state.get("baseline_watermark")
        baseline_digest = state.get("baseline_watermark_digest")
        known = (
            stage in STAGES[:-1]
            and state.get("proxy_ever_public") is False
            and proxy_continuously_isolated
            and validate_watermark(baseline) == baseline_digest
            and digest_json(current_watermark) == baseline_digest
        )
    except (OSError, RollbackSafetyError, TypeError, AttributeError):
        known = False
    return "pre_public_restore" if known else "preserve_forward_data"


class RollbackClock:
    """One persistent RTO start; retries and wall-clock rollback cannot reset it."""

    def __init__(
        self,
        path: Path,
        *,
        operation_id: str,
        release_id: str,
        wall_now: Callable[[], datetime] | None = None,
        monotonic_now: Callable[[], float] | None = None,
    ):
        if not path.is_absolute() or path == Path("/"):
            raise RollbackSafetyError("RTO state path must be bounded and absolute")
        if SAFE_ID.fullmatch(operation_id) is None or SAFE_ID.fullmatch(release_id) is None:
            raise RollbackSafetyError("rollback operation identity is invalid")
        self.path = path
        self.integrity_path = path.with_suffix(f"{path.suffix}.sha256")
        self.operation_id = operation_id
        self.release_id = release_id
        self.wall_now = wall_now or (lambda: datetime.now(tz=UTC))
        self.monotonic_now = monotonic_now or time.monotonic
        self._process_anchor = self.monotonic_now()

    def declare(self) -> dict[str, Any]:
        if self.path.exists():
            return self.read()
        state = {
            "schema_version": SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "release_id": self.release_id,
            "started_at": format_time(self.wall_now()),
            "elapsed_floor_seconds": 0,
            "completed": False,
            "rto_passed": None,
        }
        payload = canonical_json(state)
        atomic_create(self.path, payload)
        try:
            atomic_create(
                self.integrity_path,
                f"{hashlib.sha256(payload).hexdigest()}\n".encode(),
            )
        except BaseException:
            self.path.unlink(missing_ok=True)
            raise
        return state

    def read(self) -> dict[str, Any]:
        state = read_json_file(self.path)
        try:
            expected = self.integrity_path.read_text(encoding="ascii").strip()
        except OSError as error:
            raise RollbackSafetyError("rollback RTO integrity seal is missing") from error
        if (
            self.integrity_path.is_symlink()
            or not self.integrity_path.is_file()
            or SHA256.fullmatch(expected) is None
            or hashlib.sha256(canonical_json(state)).hexdigest() != expected
        ):
            raise RollbackSafetyError("rollback RTO integrity seal mismatch")
        if (
            set(state)
            != {
                "schema_version",
                "operation_id",
                "release_id",
                "started_at",
                "elapsed_floor_seconds",
                "completed",
                "rto_passed",
            }
            or state["schema_version"] != SCHEMA_VERSION
            or state["operation_id"] != self.operation_id
            or state["release_id"] != self.release_id
            or not isinstance(state["elapsed_floor_seconds"], int)
            or isinstance(state["elapsed_floor_seconds"], bool)
            or state["elapsed_floor_seconds"] < 0
            or not isinstance(state["completed"], bool)
            or state["rto_passed"] not in {None, True, False}
        ):
            raise RollbackSafetyError("rollback RTO record is invalid")
        parse_time(state["started_at"], field="rollback.started_at")
        return state

    def elapsed(self) -> int:
        state = self.declare()
        wall_elapsed = int(
            (
                self.wall_now() - parse_time(state["started_at"], field="rollback.started_at")
            ).total_seconds()
        )
        process_elapsed = int(self.monotonic_now() - self._process_anchor)
        elapsed = max(int(state["elapsed_floor_seconds"]), wall_elapsed, process_elapsed, 0)
        if elapsed != state["elapsed_floor_seconds"]:
            state["elapsed_floor_seconds"] = elapsed
            self._write(state)
        return elapsed

    def _write(self, state: Mapping[str, Any]) -> None:
        payload = canonical_json(state)
        atomic_write(self.path, payload)
        atomic_write(
            self.integrity_path,
            f"{hashlib.sha256(payload).hexdigest()}\n".encode(),
        )

    def complete_after_external_readiness(
        self,
        *,
        external_ready_at: datetime | None = None,
    ) -> tuple[int, bool]:
        state = self.read()
        if state["completed"]:
            return int(state["elapsed_floor_seconds"]), bool(state["rto_passed"])
        if external_ready_at is None:
            elapsed = self.elapsed()
        else:
            if external_ready_at.tzinfo is None:
                raise RollbackSafetyError("external readiness time must be timezone-aware")
            wall_elapsed = int(
                (
                    external_ready_at.astimezone(UTC)
                    - parse_time(state["started_at"], field="rollback.started_at")
                ).total_seconds()
            )
            if wall_elapsed < 0:
                raise RollbackSafetyError("external readiness predates rollback declaration")
            elapsed = max(int(state["elapsed_floor_seconds"]), wall_elapsed)
        state = self.read()
        state["elapsed_floor_seconds"] = elapsed
        state["completed"] = True
        state["rto_passed"] = elapsed <= RTO_LIMIT_SECONDS
        self._write(state)
        if elapsed > RTO_LIMIT_SECONDS:
            atomic_write(
                self.path.with_suffix(f"{self.path.suffix}.rto-alert.json"),
                canonical_json(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "operation_id": self.operation_id,
                        "release_id": self.release_id,
                        "elapsed_seconds": elapsed,
                        "limit_seconds": RTO_LIMIT_SECONDS,
                        "action": "alert_only_no_automatic_data_loss",
                    }
                ),
            )
        return elapsed, elapsed <= RTO_LIMIT_SECONDS


class AuditLog:
    """Append-only, hash-chained rollback operations audit outside the app DB."""

    def __init__(self, path: Path):
        if not path.is_absolute() or path == Path("/"):
            raise RollbackSafetyError("audit path must be bounded and absolute")
        self.path = path
        self.anchor_path = path.with_suffix(f"{path.suffix}.anchor")

    def _events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        if self.path.is_symlink() or not self.path.is_file():
            raise RollbackSafetyError("audit log is not a regular file")
        events: list[dict[str, Any]] = []
        previous = "0" * 64
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise RollbackSafetyError("audit log contains invalid JSON") from error
            if not isinstance(event, dict):
                raise RollbackSafetyError("audit event is invalid")
            observed = event.pop("event_sha256", None)
            if event.get("previous_sha256") != previous or observed != digest_json(event):
                raise RollbackSafetyError("audit chain was modified or truncated")
            event["event_sha256"] = observed
            events.append(event)
            previous = str(observed)
        if not self.anchor_path.is_file():
            raise RollbackSafetyError("audit head anchor is missing")
        anchor = read_json_file(self.anchor_path)
        if anchor != {"event_count": len(events), "head_sha256": previous}:
            raise RollbackSafetyError("audit chain was truncated or its head changed")
        return events

    def verify(self) -> list[dict[str, Any]]:
        return self._events()

    def append(self, event: Mapping[str, Any]) -> str:
        if any(word in str(key).lower() for key in event for word in SENSITIVE_AUDIT_WORDS):
            raise RollbackSafetyError("sensitive field is forbidden in rollback audit")
        existing = self._events()
        previous = existing[-1]["event_sha256"] if existing else "0" * 64
        body = dict(event)
        body["previous_sha256"] = previous
        body["event_sha256"] = digest_json(body)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(descriptor, "ab") as stream:
                stream.write(canonical_json(body))
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(self.path.parent)
        finally:
            pass
        atomic_write(
            self.anchor_path,
            canonical_json(
                {
                    "event_count": len(existing) + 1,
                    "head_sha256": body["event_sha256"],
                }
            ),
        )
        return str(body["event_sha256"])


def validate_lossy_authorization(
    authorization: Mapping[str, Any],
    *,
    record: Mapping[str, Any],
    operation_id: str,
    environment_id: str,
    operator: str,
    supplied_challenge: str,
    onsite_retention_sha256: str,
    used_challenges: Path,
    now: datetime,
    consume: bool = True,
) -> str:
    """Bind one approved loss window to one operation before PR2A is reachable."""

    required = {
        "schema_version",
        "release_id",
        "release_identity",
        "operation_id",
        "environment_id",
        "backup_id",
        "operator",
        "authorization_record",
        "approved_at",
        "expires_at",
        "one_time_challenge_sha256",
        "loss_start",
        "loss_end",
        "onsite_retention_sha256",
        "reconciliation_plan",
        "approval",
    }
    if set(authorization) != required or authorization.get("schema_version") != SCHEMA_VERSION:
        raise RollbackSafetyError("lossy authorization schema mismatch")
    for value, field in (
        (operation_id, "operation_id"),
        (environment_id, "environment_id"),
        (operator, "operator"),
    ):
        if SAFE_ID.fullmatch(value) is None:
            raise RollbackSafetyError(f"lossy {field} is invalid")
    identity = validate_release_record(record)
    if record["compatibility"]["verdict"] != "incompatible":
        raise RollbackSafetyError("lossy recovery is forbidden for a compatible release")
    backup = record["pre_release_backup"]
    bindings = {
        "release_id": record["release_id"],
        "release_identity": identity,
        "operation_id": operation_id,
        "environment_id": environment_id,
        "backup_id": backup["backup_id"],
        "operator": operator,
        "onsite_retention_sha256": onsite_retention_sha256,
    }
    if any(authorization.get(key) != value for key, value in bindings.items()):
        raise RollbackSafetyError("lossy authorization identity mismatch")
    _safe_id(authorization, "authorization_record")
    _required_text(authorization, "reconciliation_plan")
    _validate_approval(authorization["approval"], field="lossy_operation")
    approved_at = parse_time(authorization["approved_at"], field="lossy.approved_at")
    expires_at = parse_time(authorization["expires_at"], field="lossy.expires_at")
    loss_start = parse_time(authorization["loss_start"], field="lossy.loss_start")
    loss_end = parse_time(authorization["loss_end"], field="lossy.loss_end")
    if expires_at <= approved_at or now > expires_at or loss_end <= loss_start or now > loss_end:
        raise RollbackSafetyError("lossy authorization timing is invalid or expired")
    preapproved = record["pre_publication_plan"]["lossy_recovery"]
    if not isinstance(preapproved, dict) or preapproved.get("preapproved") is not True:
        raise RollbackSafetyError("release has no preapproved lossy recovery plan")
    if (
        authorization["authorization_record"] != preapproved["authorization_record"]
        or loss_start < parse_time(preapproved["loss_start"], field="plan.loss_start")
        or loss_end > parse_time(preapproved["loss_end"], field="plan.loss_end")
    ):
        raise RollbackSafetyError("lossy authorization exceeds preapproved loss window")
    expected_challenge = authorization["one_time_challenge_sha256"]
    if (
        not supplied_challenge
        or not isinstance(expected_challenge, str)
        or SHA256.fullmatch(expected_challenge) is None
        or hashlib.sha256(supplied_challenge.encode()).hexdigest() != expected_challenge
    ):
        raise RollbackSafetyError("lossy authorization challenge mismatch")
    if (
        not isinstance(onsite_retention_sha256, str)
        or SHA256.fullmatch(onsite_retention_sha256) is None
    ):
        raise RollbackSafetyError("authenticated onsite retention proof is required")
    if (
        not used_challenges.is_absolute()
        or used_challenges == Path("/")
        or not used_challenges.is_dir()
        or used_challenges.is_symlink()
        or used_challenges.stat().st_mode & 0o077
    ):
        raise RollbackSafetyError("used-challenge ledger must be a bounded mode-0700 directory")
    challenge_marker = used_challenges / f"{expected_challenge}.used"
    if challenge_marker.exists():
        raise RollbackSafetyError("lossy authorization challenge was already used")
    if consume:
        atomic_create(
            challenge_marker,
            canonical_json(
                {
                    "release_id": record["release_id"],
                    "operation_id": operation_id,
                    "authorization_record": authorization["authorization_record"],
                    "used_at": format_time(now),
                }
            ),
        )
    return str(backup["backup_id"])


def build_runtime_identity(record: Mapping[str, Any], *, version: str) -> dict[str, Any]:
    """Build the only app/config pointer that a path-two switch may atomically replace."""

    if version not in {"stable", "candidate"}:
        raise RollbackSafetyError("runtime version must be stable or candidate")
    validate_release_record(record)
    artifacts = record[version]
    return {
        "schema_version": SCHEMA_VERSION,
        "release_id": record["release_id"],
        "release_identity": release_identity(record),
        "version": version,
        "app_image": artifacts["images"]["app"],
        "app_config": artifacts["app_config"],
        # Current secret references are intentionally absent: the running environment
        # retains them and this pointer cannot restore old values or references.
    }


def switch_runtime_identity(path: Path, identity: Mapping[str, Any]) -> None:
    """Atomically switch app image and allowlisted config as one indivisible identity."""

    if set(identity) != {
        "schema_version",
        "release_id",
        "release_identity",
        "version",
        "app_image",
        "app_config",
    }:
        raise RollbackSafetyError("runtime identity schema mismatch")
    _exact_image(identity["app_image"], field="runtime.app_image")
    if identity["version"] not in {"stable", "candidate"}:
        raise RollbackSafetyError("runtime identity version is invalid")
    configs = identity["app_config"]
    if not isinstance(configs, dict) or not configs or set(configs) - ALLOWED_APP_CONFIG:
        raise RollbackSafetyError("runtime identity config is outside allowlist")
    atomic_write(path, canonical_json(identity))
