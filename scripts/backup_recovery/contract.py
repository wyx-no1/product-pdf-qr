"""Machine-readable PR2A contract loading and fail-closed validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ContractError(ValueError):
    """The deployment contract is incomplete or unsafe."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def load_contract(path: Path) -> dict[str, Any]:
    """Load and validate every G03-P01 value used by executable code."""

    try:
        document = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read backup contract: {error}") from error
    validate_contract(document)
    return document


def validate_contract(document: dict[str, Any]) -> None:
    """Reject placeholders and safety-boundary drift."""

    _require(document.get("schema_version") == 1, "unsupported contract schema")
    timezone = document.get("business_timezone")
    _require(timezone == "Asia/Shanghai", "business timezone must remain Asia/Shanghai")
    try:
        ZoneInfo(str(timezone))
    except ZoneInfoNotFoundError as error:
        raise ContractError("business timezone is unavailable") from error

    schedule = document["schedule"]
    _require(schedule["finalizer_local_time"] == "02:30:00", "finalizer time changed")
    _require(
        schedule["precopy_local_times"]
        == ["06:30:00", "10:30:00", "14:30:00", "18:30:00", "22:30:00"],
        "precopy schedule changed",
    )
    _require(
        schedule["missed_finalizer"] == "alert_and_wait_for_next_window",
        "missed finalizer must not stop daytime service",
    )
    _require(schedule["week_starts_on"] == "monday", "week boundary changed")

    model = document["object_model"]
    _require(model["plaintext_digest"] == "sha256", "content addressing must use SHA-256")
    _require(
        model["kind"] == "encrypted_content_addressed_objects_with_encrypted_manifest",
        "object model changed",
    )
    _require(
        0 < int(model["max_tmpfs_chunk_bytes"]) <= 67_108_864,
        "tmpfs plaintext chunk limit is unsafe",
    )

    capacity = document["capacity_baseline"]
    _require(int(capacity["historical_file_bytes"]) >= 250 * 1024**3, "capacity too small")
    _require(int(capacity["historical_file_count"]) >= 20_000, "file count too small")
    _require(int(capacity["database_rows"]) >= 1_000_000, "database row baseline too small")
    _require(int(capacity["finalizer_hard_limit_seconds"]) == 900, "window must be 15 min")
    _require(int(capacity["rto_hard_limit_seconds"]) == 14_400, "RTO must be four hours")
    _require(int(capacity["safety_margin_percent"]) >= 25, "capacity safety margin too small")

    encryption = document["encryption"]
    _require(
        (encryption["tool"], encryption["version"], encryption["format"])
        == ("age", "1.3.1", "age-v1"),
        "age tool contract changed",
    )
    _require(encryption["recipient_type"] == "X25519", "recipient type changed")
    _require(
        encryption["production_private_key_location"] == "offline_authorized_custodian_only",
        "production may not hold a decrypt key",
    )
    manifest_authentication = document["manifest_authentication"]
    _require(
        (
            manifest_authentication["algorithm"],
            manifest_authentication["key_bytes"],
            manifest_authentication["upload_identity_has_signing_key"],
        )
        == ("ed25519", 32, False),
        "manifest authentication boundary changed",
    )
    _require(
        manifest_authentication["backup_signing_authority_location"]
        == "production_backup_secret_only",
        "manifest authentication authority escaped backup",
    )
    _require(
        manifest_authentication["restore_verifier_location"] == "one_time_restore_public_key_only",
        "manifest restore verifier location changed",
    )
    _require(
        manifest_authentication["retention_verifier_location"]
        == "independent_authorized_environment_public_key_only",
        "manifest retention verifier location changed",
    )
    restore_verification = document["restore_verification_authentication"]
    _require(
        (
            restore_verification["algorithm"],
            restore_verification["key_bytes"],
            restore_verification["backup_upload_identity_has_signing_key"],
        )
        == ("ed25519", 32, False),
        "restore verification authentication boundary changed",
    )
    _require(
        restore_verification["signing_authority_location"] == "one_time_restore_secret_only",
        "restore verification authority escaped restore",
    )
    _require(
        restore_verification["retention_verifier_location"]
        == "independent_authorized_environment_public_key_only",
        "restore verification public key location changed",
    )

    remote = document["remote"]
    _require(remote["category"] == "S3-compatible object storage", "remote category changed")
    fault_domain = remote["production_fault_domain"]
    for boundary in (
        "different_host",
        "different_account",
        "different_region",
        "different_storage_lifecycle",
    ):
        _require(fault_domain.get(boundary) is True, f"remote fault domain lacks {boundary}")
    forbidden = set(remote["upload_identity_forbidden_permissions"])
    _require(
        {"DeleteObject", "DeleteObjectVersion", "BypassGovernanceRetention"} <= forbidden,
        "upload identity delete isolation weakened",
    )
    _require(
        set(remote["delete_identity_permissions"])
        == {"ListBucket", "GetObject", "PutObject", "DeleteObject"},
        "delete identity journal permissions changed",
    )
    _require(
        {"DeleteObjectVersion", "PutObjectRetention", "BypassGovernanceRetention"}
        <= set(remote["delete_identity_forbidden_permissions"]),
        "delete identity retention bypass isolation weakened",
    )
    _require(remote["versioning_required"] is True, "remote versioning is required")
    _require(remote["object_lock_required"] is True, "remote object lock is required")

    retention = document["retention"]
    _require(
        (retention["daily_days"], retention["weekly_weeks"], retention["monthly_months"])
        == (14, 8, 6),
        "retention values changed",
    )
    _require(retention["unique_verified_never_delete"] is True, "unique verified is unprotected")
    _require(
        retention["verified_rule"] == "remote_download_plus_complete_isolated_restore_only",
        "verified rule weakened",
    )
    _require(
        retention["delete_key_authority"] == "ed25519_authenticated_manifest_only",
        "retention delete-key authority weakened",
    )
    _require(
        retention["deletion_journal_direction"] == "delete_resume_only_never_completion",
        "retention deletion journal became reversible",
    )

    database = document["database"]
    _require(database["major_version"] == 16, "PostgreSQL major version must be 16")
    _require(database["backup_role"] == "app_backup", "scheduled backup role changed")
    _require(database["existing_database_restore_role"] == "app_migrate", "restore owner changed")
    _require(database["restore_superuser_required"] is False, "restore must not require superuser")

    restore = document["restore"]
    _require(
        restore["order"]
        == [
            "remote_download",
            "authenticated_preflight",
            "stop_proxy",
            "stop_app",
            "encrypted_site_retention",
            "database",
            "files",
            "offline_full_validation",
            "isolated_app_validation",
            "proxy_last",
            "external_readiness",
        ],
        "restore order changed",
    )
    _require(
        len(restore["production_confirmation_binding"]) == 7,
        "production confirmation is not strongly bound",
    )
    _require(
        bool(document.get("recoverable_config_allowlist")), "config recovery allowlist is empty"
    )
    _require(bool(document.get("secret_references_only")), "secret boundary is missing")
