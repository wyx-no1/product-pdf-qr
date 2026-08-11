"""PR2A contract, identity, retention, and fail-closed unit tests."""

from __future__ import annotations

import io
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.backup_recovery.backup import BACKUP_FAILURE_STAGES, new_backup_id
from scripts.backup_recovery.contract import ContractError, load_contract, validate_contract
from scripts.backup_recovery.model import (
    RestoreGuard,
    SafetyError,
    authenticate_manifest,
    canonical_json,
    generation_tags,
    inventory,
    retention_decisions,
    retention_record_from_authenticated_manifest,
    validate_manifest,
)
from scripts.backup_recovery.remote import LocalRemote, remote_from_environment
from scripts.backup_recovery.restore import RESTORE_FAILURE_STAGES

ROOT = Path(__file__).parents[2]
CONTRACT_PATH = ROOT / "deploy" / "backup" / "contract.json"
MANIFEST_AUTHENTICATION_KEY = b"k" * 32
MANIFEST_VERIFICATION_KEY = (
    Ed25519PrivateKey.from_private_bytes(MANIFEST_AUTHENTICATION_KEY)
    .public_key()
    .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
)
MANIFEST_AUTHENTICATION_KEY_ID = "manifest-auth-test"


def _backup_id(suffix: str) -> str:
    return f"20260101T000000Z-{suffix * 32}"


def _manifest() -> dict[str, object]:
    backup_id = _backup_id("a")
    return authenticate_manifest(
        {
            "schema_version": 1,
            "backup_id": backup_id,
            "started_at": "2026-01-01T00:00:00.000001Z",
            "frozen_at": "2026-01-01T00:00:01.000001Z",
            "completed_at": "2026-01-01T00:00:02.000001Z",
            "source_commit": "1" * 40,
            "images": {"app": f"app:v1@sha256:{'2' * 64}"},
            "config_hashes": {"compose.prod.yaml": "3" * 64},
            "alembic_revision": "20260804_0002",
            "tools": {"age": "1.3.1"},
            "volume_name": "product_pdf_qr_files",
            "database_name": "synthetic",
            "recipient_key_id": "synthetic-key",
            "objects": [
                {
                    "name": "database",
                    "key": f"points/{backup_id}/database.age",
                    "backup_id": backup_id,
                    "plaintext_size": 10,
                    "plaintext_sha256": "4" * 64,
                    "ciphertext_size": 20,
                    "ciphertext_sha256": "5" * 64,
                },
                {
                    "name": "config",
                    "key": f"points/{backup_id}/config.age",
                    "backup_id": backup_id,
                    "plaintext_size": 10,
                    "plaintext_sha256": "4" * 64,
                    "ciphertext_size": 20,
                    "ciphertext_sha256": "5" * 64,
                },
                {
                    "name": "file:files/aa/document.pdf",
                    "key": "objects/sha256/" + "4" * 64 + "/synthetic.age",
                    "backup_id": backup_id,
                    "plaintext_size": 10,
                    "plaintext_sha256": "4" * 64,
                    "ciphertext_size": 20,
                    "ciphertext_sha256": "5" * 64,
                },
            ],
            "files": [{"path": "files/aa/document.pdf", "size": 10, "sha256": "4" * 64}],
        },
        key=MANIFEST_AUTHENTICATION_KEY,
        key_id=MANIFEST_AUTHENTICATION_KEY_ID,
    )


def test_executable_contract_locks_all_g03_p01_values() -> None:
    contract = load_contract(CONTRACT_PATH)

    assert contract["business_timezone"] == "Asia/Shanghai"
    assert contract["schedule"]["finalizer_local_time"] == "02:30:00"
    assert contract["schedule"]["missed_finalizer"] == "alert_and_wait_for_next_window"
    assert contract["object_model"]["plaintext_digest"] == "sha256"
    assert contract["encryption"]["tool"] == "age"
    assert contract["manifest_authentication"] == {
        "algorithm": "ed25519",
        "key_bytes": 32,
        "key_id_required": True,
        "backup_signing_authority_location": "production_backup_secret_only",
        "restore_verifier_location": "one_time_restore_public_key_only",
        "retention_verifier_location": "independent_authorized_environment_public_key_only",
        "upload_identity_has_signing_key": False,
    }
    assert contract["restore_verification_authentication"] == {
        "algorithm": "ed25519",
        "key_bytes": 32,
        "key_id_required": True,
        "signing_authority_location": "one_time_restore_secret_only",
        "retention_verifier_location": "independent_authorized_environment_public_key_only",
        "backup_upload_identity_has_signing_key": False,
    }
    assert contract["remote"]["category"] == "S3-compatible object storage"
    assert contract["capacity_baseline"]["historical_file_bytes"] == 250 * 1024**3
    assert contract["retention"] == {
        "daily_days": 14,
        "weekly_weeks": 8,
        "monthly_months": 6,
        "delete_only_after_all_generations_expire": True,
        "unique_verified_never_delete": True,
        "verified_rule": "remote_download_plus_complete_isolated_restore_only",
        "delete_key_authority": "ed25519_authenticated_manifest_only",
        "deletion_journal_direction": "delete_resume_only_never_completion",
    }


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("business_timezone",), "UTC"),
        (("capacity_baseline", "finalizer_hard_limit_seconds"), 901),
        (("capacity_baseline", "rto_hard_limit_seconds"), 14401),
        (("encryption", "version"), "latest"),
        (("manifest_authentication", "upload_identity_has_signing_key"), True),
        (
            (
                "restore_verification_authentication",
                "backup_upload_identity_has_signing_key",
            ),
            True,
        ),
        (("remote", "category"), "local directory"),
        (("remote", "delete_identity_permissions"), ["DeleteObject"]),
        (("retention", "daily_days"), 13),
        (("retention", "unique_verified_never_delete"), False),
        (("retention", "delete_key_authority"), "completion_marker_object_keys"),
        (("retention", "deletion_journal_direction"), "restore_completion"),
        (("database", "backup_role"), "app_migrate"),
    ),
)
def test_contract_rejects_safety_boundary_drift(path: tuple[str, ...], value: object) -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    target = contract
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(ContractError):
        validate_contract(contract)


def test_inventory_is_content_addressed_not_metadata_addressed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    path = source / "same.pdf"
    path.write_bytes(b"content-one")
    first = inventory(source)
    original_mode = path.stat().st_mode
    original_time = path.stat().st_mtime_ns
    path.write_bytes(b"content-two")
    path.chmod(stat.S_IMODE(original_mode))
    os.utime(path, ns=(original_time, original_time))
    assert path.stat().st_size == len(b"content-two")
    assert path.stat().st_mtime_ns == original_time
    second = inventory(source)

    assert first[0]["path"] == second[0]["path"]
    assert first[0]["size"] == second[0]["size"]
    assert first[0]["sha256"] != second[0]["sha256"]


def test_inventory_rejects_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = source / "target.pdf"
    target.write_bytes(b"synthetic")
    (source / "link.pdf").symlink_to(target)

    with pytest.raises(SafetyError, match="regular file"):
        inventory(source)


def test_storage_inventory_excludes_unpublished_upload_temporaries(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "temporary").mkdir(parents=True)
    (source / "files").mkdir()
    (source / "temporary" / "upload-rejected.part").write_bytes(b"untrusted")
    (source / "files" / "published.pdf").write_bytes(b"published")

    result = inventory(source, excluded_top_level_names={"temporary"})

    assert [item["path"] for item in result] == ["files/published.pdf"]


def test_backup_id_is_unique_and_path_safe() -> None:
    now = datetime(2026, 8, 7, 2, 30, tzinfo=UTC)
    first = new_backup_id(now)
    second = new_backup_id(now)

    assert first != second
    assert first.startswith("20260807T023000Z-")
    assert "/" not in first


def test_manifest_rejects_mixed_bundle_and_unsafe_paths() -> None:
    manifest = _manifest()
    validate_manifest(manifest)
    manifest["objects"][0]["backup_id"] = _backup_id("b")  # type: ignore[index]
    with pytest.raises(SafetyError, match="mixed"):
        validate_manifest(manifest)

    manifest = _manifest()
    manifest["files"][0]["path"] = "../escape.pdf"  # type: ignore[index]
    with pytest.raises(SafetyError, match="unsafe"):
        validate_manifest(manifest)


def test_upload_identity_cannot_forge_retention_delete_authority() -> None:
    manifest = _manifest()
    backup_id = str(manifest["backup_id"])
    frozen = datetime.fromisoformat(str(manifest["frozen_at"]).replace("Z", "+00:00"))
    manifest["generations"] = sorted(generation_tags(frozen, "Asia/Shanghai"))
    objects = cast(list[dict[str, Any]], manifest["objects"])
    objects[0]["key"] = f"production/points/{backup_id}/database.dump.age"
    objects[1]["key"] = f"production/points/{backup_id}/config.tar.age"
    digest = str(objects[2]["plaintext_sha256"])
    objects[2]["key"] = f"production/objects/sha256/{digest}/synthetic-key.age"
    manifest = authenticate_manifest(
        {key: value for key, value in manifest.items() if key != "authentication"},
        key=MANIFEST_AUTHENTICATION_KEY,
        key_id=MANIFEST_AUTHENTICATION_KEY_ID,
    )

    record = retention_record_from_authenticated_manifest(
        manifest,
        backup_id=backup_id,
        prefix="production",
        timezone="Asia/Shanghai",
        verification_key=MANIFEST_VERIFICATION_KEY,
        authentication_key_id=MANIFEST_AUTHENTICATION_KEY_ID,
    )

    assert record["source"] == "ed25519_authenticated_manifest"
    assert record["object_keys"] == sorted(
        {
            f"production/points/{backup_id}/database.dump.age",
            f"production/points/{backup_id}/config.tar.age",
            f"production/points/{backup_id}/manifest.json.age",
            f"production/objects/sha256/{digest}/synthetic-key.age",
            f"production/objects/sha256/{digest}/synthetic-key.json",
        }
    )

    forged_manifest = dict(manifest)
    forged_objects = [dict(item) for item in cast(list[dict[str, Any]], manifest["objects"])]
    forged_objects[2]["key"] = f"production/complete/{backup_id}.json"
    forged_manifest["objects"] = forged_objects
    with pytest.raises(SafetyError, match="manifest authentication failed"):
        retention_record_from_authenticated_manifest(
            forged_manifest,
            backup_id=backup_id,
            prefix="production",
            timezone="Asia/Shanghai",
            verification_key=MANIFEST_VERIFICATION_KEY,
            authentication_key_id=MANIFEST_AUTHENTICATION_KEY_ID,
        )
    with pytest.raises(SafetyError, match="manifest authentication failed"):
        retention_record_from_authenticated_manifest(
            manifest,
            backup_id=backup_id,
            prefix="production",
            timezone="Asia/Shanghai",
            verification_key=b"u" * 32,
            authentication_key_id=MANIFEST_AUTHENTICATION_KEY_ID,
        )
    with pytest.raises(SafetyError, match="authentication authority mismatch"):
        retention_record_from_authenticated_manifest(
            manifest,
            backup_id=backup_id,
            prefix="production",
            timezone="Asia/Shanghai",
            verification_key=MANIFEST_VERIFICATION_KEY,
            authentication_key_id="upload-controlled-key-id",
        )

    explicitly_authenticated_forgery = authenticate_manifest(
        {key: value for key, value in forged_manifest.items() if key != "authentication"},
        key=MANIFEST_AUTHENTICATION_KEY,
        key_id=MANIFEST_AUTHENTICATION_KEY_ID,
    )
    with pytest.raises(SafetyError, match="authenticated manifest object key mismatch"):
        retention_record_from_authenticated_manifest(
            explicitly_authenticated_forgery,
            backup_id=backup_id,
            prefix="production",
            timezone="Asia/Shanghai",
            verification_key=MANIFEST_VERIFICATION_KEY,
            authentication_key_id=MANIFEST_AUTHENTICATION_KEY_ID,
        )


def test_restore_guard_binds_target_backup_operator_window_and_challenge() -> None:
    backup_id = _backup_id("a")
    guard = RestoreGuard(
        environment_id="synthetic-production-01",
        backup_id=backup_id,
        operator_id="operator-7",
        approved_data_loss_window="2026-08-07T02:30Z..2026-08-07T03:00Z",
        authorization_record="change-1234",
        expires_at=datetime.now(tz=UTC) + timedelta(minutes=5),
        challenge="bound-one-time-challenge",
    )
    guard.validate(
        target_environment_id="synthetic-production-01",
        supplied_challenge="bound-one-time-challenge",
    )

    with pytest.raises(SafetyError, match="marker mismatch"):
        guard.validate(
            target_environment_id="other-target",
            supplied_challenge="bound-one-time-challenge",
        )
    with pytest.raises(SafetyError, match="challenge mismatch"):
        guard.validate(
            target_environment_id="synthetic-production-01",
            supplied_challenge="YES",
        )


def test_expired_restore_authorization_is_rejected() -> None:
    guard = RestoreGuard(
        environment_id="synthetic",
        backup_id=_backup_id("a"),
        operator_id="operator",
        approved_data_loss_window="one-hour",
        authorization_record="change-1",
        expires_at=datetime.now(tz=UTC) - timedelta(seconds=1),
        challenge="one-time",
    )

    with pytest.raises(SafetyError, match="expired"):
        guard.validate(target_environment_id="synthetic", supplied_challenge="one-time")


def test_generation_tags_use_shanghai_monday_and_month_first() -> None:
    frozen = datetime(2026, 6, 30, 16, 30, tzinfo=UTC)

    assert generation_tags(frozen, "Asia/Shanghai") == {
        "daily:2026-07-01",
        "monthly:2026-07",
    }

    monday = datetime(2026, 8, 2, 18, 30, tzinfo=UTC)
    assert "weekly:2026-W32" in generation_tags(monday, "Asia/Shanghai")


def test_retention_boundaries_overlap_and_unique_verified_protection() -> None:
    now = datetime(2026, 8, 20, 2, 30, tzinfo=UTC)
    recent = _backup_id("a")
    boundary = _backup_id("b")
    monthly = _backup_id("c")
    unique = _backup_id("d")
    points = [
        {
            "backup_id": recent,
            "frozen_at": "2026-08-19T02:30:00Z",
            "generations": ["daily:2026-08-19"],
        },
        {
            "backup_id": boundary,
            "frozen_at": "2026-08-06T02:30:00Z",
            "generations": ["daily:2026-08-06"],
        },
        {
            "backup_id": monthly,
            "frozen_at": "2026-04-01T02:30:00Z",
            "generations": ["daily:2026-04-01", "monthly:2026-04"],
        },
        {
            "backup_id": unique,
            "frozen_at": "2025-01-01T02:30:00Z",
            "generations": ["daily:2025-01-01", "monthly:2025-01"],
        },
    ]

    decisions = retention_decisions(
        points,
        now=now,
        timezone="Asia/Shanghai",
        unique_verified_backup_id=unique,
    )

    assert decisions[recent] == "keep:daily"
    assert decisions[boundary] == "delete:expired_all"
    assert decisions[monthly] == "keep:monthly"
    assert decisions[unique] == "keep:unique_verified"


def test_local_remote_is_immutable_and_partial_name_never_appears(tmp_path: Path) -> None:
    remote = LocalRemote(tmp_path)
    size, digest = remote.upload_stream(io.BytesIO(b"ciphertext"), "complete/point.age")

    assert remote.size_and_sha256("complete/point.age") == (size, digest)
    assert remote.list_keys("complete") == ["complete/point.age"]
    assert not any("uploading" in key for key in remote.list_keys("complete"))
    with pytest.raises(SafetyError, match="immutable"):
        remote.upload_stream(io.BytesIO(b"replacement"), "complete/point.age")


def test_local_remote_requires_explicit_synthetic_marker(tmp_path: Path) -> None:
    with pytest.raises(SafetyError, match="synthetic"):
        remote_from_environment(f"local:{tmp_path}", synthetic=False, config=None)

    assert isinstance(
        remote_from_environment(f"local:{tmp_path}", synthetic=True, config=None),
        LocalRemote,
    )


def test_canonical_json_is_stable_and_newline_terminated() -> None:
    assert canonical_json({"b": 2, "a": 1}) == b'{"a":1,"b":2}\n'


def test_failure_injection_matrices_are_complete_and_not_weakened() -> None:
    assert BACKUP_FAILURE_STAGES == (
        "files",
        "dump",
        "manifest",
        "encryption",
        "upload",
    )
    assert RESTORE_FAILURE_STAGES == (
        "decrypt",
        "preflight",
        "site_retention",
        "database",
        "files",
        "offline_validation",
        "isolated_functional_validation",
        "proxy",
    )
