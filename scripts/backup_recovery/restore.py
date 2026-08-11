"""Fail-closed recovery preflight, restore state machine, and validation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.backup_recovery.crypto import (
    AgeCipher,
    decrypt_file_to_hash,
    decrypt_small,
    decrypt_to_path,
    decrypt_to_process,
)
from scripts.backup_recovery.model import (
    RestoreGuard,
    SafetyError,
    atomic_write,
    authenticate_restore_verification,
    canonical_json,
    format_time,
    inventory,
    utc_now,
    validate_backup_id,
    validate_manifest,
    verify_manifest_authentication,
    verify_restore_verification,
)
from scripts.backup_recovery.remote import LocalRemote, Remote

STAGE_ORDER = {
    "declared": 0,
    "preflight_complete": 1,
    "site_retained": 2,
    "database_restored": 3,
    "files_complete": 4,
    "offline_validated": 5,
    "isolated_functional_validated": 6,
    "proxy_authorized": 7,
    "external_ready": 8,
    "rolled_back": 9,
}
RESTORE_FAILURE_STAGES = (
    "decrypt",
    "preflight",
    "site_retention",
    "database",
    "files",
    "offline_validation",
    "isolated_functional_validation",
    "proxy",
)
IMAGE_ENVIRONMENT = {
    "app": "APP_IMAGE",
    "db": "DB_IMAGE",
    "proxy": "PROXY_IMAGE",
    "certbot": "CERTBOT_IMAGE",
    "backup_recovery": "BACKUP_IMAGE",
}


class RestoreEngine:
    """Restore one remote-verified bundle without exposing partial state."""

    def __init__(
        self,
        *,
        contract: dict[str, Any],
        remote: Remote,
        remote_prefix: str,
        state_root: Path,
        file_root: Path,
        identity: Path,
        recipient: str,
        recipient_key_id: str,
        manifest_verification_key: bytes,
        manifest_authentication_key_id: str,
        restore_verification_authentication_key: bytes,
        restore_verification_authentication_key_id: str,
        environment_id: str,
        environment_marker: Path,
        authorization: Path,
        confirmation: str,
        repository_root: Path,
    ) -> None:
        self.contract = contract
        self.remote = remote
        self.prefix = remote_prefix.strip("/")
        self.state_root = state_root
        self.file_root = file_root
        self.identity = identity
        self.recipient = recipient
        self.recipient_key_id = recipient_key_id
        self.manifest_verification_key = manifest_verification_key
        self.manifest_authentication_key_id = manifest_authentication_key_id
        self.restore_verification_authentication_key = restore_verification_authentication_key
        self.restore_verification_authentication_key_id = restore_verification_authentication_key_id
        self.environment_id = environment_id
        self.environment_marker = environment_marker
        self.authorization = authorization
        self.confirmation = confirmation
        self.repository_root = repository_root
        if not self.prefix:
            raise SafetyError("remote prefix is required")
        self._validate_identity_inputs()

    def _validate_identity_inputs(self) -> None:
        if not self.environment_id.strip():
            raise SafetyError("target environment id is empty")
        if not self.environment_marker.is_file():
            raise SafetyError("target environment marker is missing")
        marker = self.environment_marker.read_text(encoding="utf-8").strip()
        if marker != self.environment_id:
            raise SafetyError("target environment marker mismatch")
        if not self.identity.is_file():
            raise SafetyError("offline identity was not injected")
        if self.identity.stat().st_mode & 0o077:
            raise SafetyError("offline identity must have mode 0600 or stricter")
        try:
            authorization = json.loads(self.authorization.read_text(encoding="utf-8"))
            guard = RestoreGuard(
                environment_id=str(authorization["environment_id"]),
                backup_id=str(authorization["backup_id"]),
                operator_id=str(authorization["operator_id"]),
                approved_data_loss_window=str(authorization["approved_data_loss_window"]),
                authorization_record=str(authorization["authorization_record"]),
                expires_at=datetime.fromisoformat(
                    str(authorization["expires_at"]).replace("Z", "+00:00")
                ),
                challenge=str(authorization["one_time_challenge"]),
            )
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
            raise SafetyError("restore authorization is invalid") from error
        guard.validate(
            target_environment_id=self.environment_id,
            supplied_challenge=self.confirmation,
        )
        self.guard = guard

    def _inject(self, stage: str) -> None:
        if stage not in RESTORE_FAILURE_STAGES:
            raise SafetyError("unknown restore failure stage")
        if os.environ.get("RESTORE_FAIL_STAGE") == stage:
            raise SafetyError(f"injected restore failure: {stage}")

    def _operation_id(self) -> str:
        """Identify one authorization-bound restore attempt without storing its challenge."""

        identity = {
            "environment_id": self.guard.environment_id,
            "backup_id": self.guard.backup_id,
            "operator_id": self.guard.operator_id,
            "approved_data_loss_window": self.guard.approved_data_loss_window,
            "authorization_record": self.guard.authorization_record,
            "expires_at": format_time(self.guard.expires_at),
            "challenge_sha256": hashlib.sha256(self.guard.challenge.encode()).hexdigest(),
        }
        return hashlib.sha256(canonical_json(identity)).hexdigest()

    def _state_path(self, backup_id: str) -> Path:
        validate_backup_id(backup_id)
        return self.state_root / "restores" / backup_id / f"{self._operation_id()}.json"

    def _cache_root(self, backup_id: str) -> Path:
        validate_backup_id(backup_id)
        return self.state_root / "restore-cache" / backup_id

    def _read_state(self, backup_id: str) -> dict[str, Any]:
        path = self._state_path(backup_id)
        if not path.is_file():
            return {"backup_id": backup_id, "stage": "declared", "history": []}
        state = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        if (
            state.get("backup_id") != backup_id
            or state.get("operation_id") != self._operation_id()
            or state.get("stage") not in STAGE_ORDER
        ):
            raise SafetyError("restore checkpoint is invalid")
        self._declared_at(state)
        return state

    @staticmethod
    def _declared_at(state: dict[str, Any]) -> datetime:
        try:
            declared_at = datetime.fromisoformat(str(state["declared_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError) as error:
            raise SafetyError("restore checkpoint declaration is invalid") from error
        if declared_at.tzinfo is None:
            raise SafetyError("restore checkpoint declaration is invalid")
        return declared_at.astimezone(UTC)

    def declare(self, backup_id: str) -> dict[str, Any]:
        """Persist the one RTO start used by every retry of this restore."""

        validate_backup_id(backup_id)
        if self.guard.backup_id != backup_id:
            raise SafetyError("authorization backup_id mismatch")
        path = self._state_path(backup_id)
        if path.is_file():
            state = self._read_state(backup_id)
        else:
            declared_at = utc_now()
            state = {
                "backup_id": backup_id,
                "operation_id": self._operation_id(),
                "stage": "declared",
                "declared_at": format_time(declared_at),
                "authorization_record": self.guard.authorization_record,
                "operator_id": self.guard.operator_id,
                "history": [],
            }
            atomic_write(path, canonical_json(state))
        return {
            "backup_id": backup_id,
            "operation_id": state["operation_id"],
            "stage": state["stage"],
            "declared_at": state["declared_at"],
        }

    def _advance(self, backup_id: str, stage: str, **details: Any) -> None:
        state = self._read_state(backup_id)
        current = str(state["stage"])
        if STAGE_ORDER[stage] < STAGE_ORDER[current]:
            raise SafetyError("restore state regression refused")
        if STAGE_ORDER[stage] > STAGE_ORDER[current] + 1:
            raise SafetyError("restore stage skipped")
        if stage != current:
            state["stage"] = stage
            state["history"].append({"stage": stage, "at": format_time(utc_now()), **details})
        atomic_write(self._state_path(backup_id), canonical_json(state))

    def _completed(self, backup_id: str, stage: str) -> bool:
        current = str(self._read_state(backup_id)["stage"])
        if current == "rolled_back":
            raise SafetyError("rolled-back restore cannot be resumed")
        return STAGE_ORDER[current] >= STAGE_ORDER[stage]

    def _completion_key(self, backup_id: str) -> str:
        return f"{self.prefix}/complete/{backup_id}.json"

    def _cache_path(self, backup_id: str, key: str) -> Path:
        suffix = hashlib.sha256(key.encode()).hexdigest()
        return self._cache_root(backup_id) / f"{suffix}.age"

    def _download_verified(self, backup_id: str, *, key: str, size: int, digest: str) -> Path:
        path = self._cache_path(backup_id, key)
        if path.is_file():
            observed = _size_and_sha256(path)
            if observed == (size, digest):
                return path
            path.unlink()
        self.remote.download_file(key, path)
        if _size_and_sha256(path) != (size, digest):
            path.unlink(missing_ok=True)
            raise SafetyError("downloaded ciphertext size or SHA-256 mismatch")
        return path

    def preflight(self, backup_id: str) -> dict[str, Any]:
        """Authenticate and hash every object before stopping or writing targets."""

        self.declare(backup_id)
        self._inject("decrypt")
        try:
            completion = json.loads(self.remote.read_bytes(self._completion_key(backup_id)))
        except (json.JSONDecodeError, OSError) as error:
            raise SafetyError("completion marker is missing or invalid") from error
        if completion.get("backup_id") != backup_id:
            raise SafetyError("completion marker identity mismatch")
        if completion.get("status") != "remote_ciphertext_verified":
            raise SafetyError("backup is not remotely verified")
        manifest_cipher = self._download_verified(
            backup_id,
            key=str(completion["manifest_key"]),
            size=int(completion["manifest_ciphertext_size"]),
            digest=str(completion["manifest_ciphertext_sha256"]),
        )
        try:
            manifest = json.loads(decrypt_small(manifest_cipher, self.identity))
        except json.JSONDecodeError as error:
            raise SafetyError("authenticated manifest is invalid") from error
        if not isinstance(manifest, dict):
            raise SafetyError("authenticated manifest is not a JSON object")
        verify_manifest_authentication(
            manifest,
            key=self.manifest_verification_key,
            key_id=self.manifest_authentication_key_id,
        )
        validate_manifest(manifest)
        if manifest["backup_id"] != backup_id:
            raise SafetyError("manifest identity mismatch")
        if manifest["recipient_key_id"] != self.recipient_key_id:
            raise SafetyError("recipient key mapping mismatch")
        if not all("@sha256:" in str(reference) for reference in manifest["images"].values()):
            raise SafetyError("manifest contains a mutable image identity")
        self._validate_manifest_deployment_identity(manifest)
        if not str(manifest["tools"]["pg_dump"]).startswith("pg_dump (PostgreSQL) 16."):
            raise SafetyError("pg_dump major version is incompatible")
        if AgeCipher.version() != str(manifest["tools"]["age"]):
            raise SafetyError("age version is incompatible")
        pg_restore_version = subprocess.run(
            ["pg_restore", "--version"],
            text=True,
            capture_output=True,
            check=False,
        )
        if pg_restore_version.returncode != 0 or "PostgreSQL) 16." not in pg_restore_version.stdout:
            raise SafetyError("pg_restore major version is incompatible")
        rclone_version = subprocess.run(
            ["rclone", "version"],
            text=True,
            capture_output=True,
            check=False,
        )
        if (
            rclone_version.returncode != 0
            or not str(manifest["tools"]["rclone"]).startswith("rclone v1.74.1")
            or not rclone_version.stdout.startswith("rclone v1.74.1")
        ):
            raise SafetyError("rclone version is incompatible")

        required_cipher_bytes = sum(int(item["ciphertext_size"]) for item in manifest["objects"])
        self._require_encrypted_cache_capacity(backup_id, manifest)
        safety_percent = int(self.contract["capacity_baseline"]["safety_margin_percent"])
        self._require_target_capacity(manifest, safety_percent=safety_percent)

        self._inject("preflight")
        for item in manifest["objects"]:
            path = self._download_verified(
                backup_id,
                key=str(item["key"]),
                size=int(item["ciphertext_size"]),
                digest=str(item["ciphertext_sha256"]),
            )
            observed_plain = decrypt_file_to_hash(path, self.identity)
            expected_plain = (int(item["plaintext_size"]), str(item["plaintext_sha256"]))
            if observed_plain != expected_plain:
                raise SafetyError("authenticated plaintext object digest mismatch")
            if item["name"] == "config":
                self._validate_config_archive(path, manifest)
        manifest_path = self._cache_root(backup_id) / "manifest.control.age"
        if not manifest_path.exists():
            shutil.copyfile(manifest_cipher, manifest_path)
        if not self._completed(backup_id, "preflight_complete"):
            self._advance(backup_id, "preflight_complete")
        return {
            "backup_id": backup_id,
            "stage": "preflight_complete",
            "object_count": len(manifest["objects"]),
            "ciphertext_bytes": required_cipher_bytes,
        }

    def _missing_ciphertext_bytes(self, backup_id: str, manifest: dict[str, Any]) -> int:
        missing = 0
        for item in manifest["objects"]:
            path = self._cache_path(backup_id, str(item["key"]))
            expected = (int(item["ciphertext_size"]), str(item["ciphertext_sha256"]))
            if path.is_symlink():
                raise SafetyError("encrypted cache contains an unsafe object")
            if path.is_file() and _size_and_sha256(path) == expected:
                continue
            if path.exists():
                if not path.is_file():
                    raise SafetyError("encrypted cache contains an unsafe object")
                path.unlink()
            missing += expected[0]
        return missing

    def _require_encrypted_cache_capacity(self, backup_id: str, manifest: dict[str, Any]) -> None:
        missing = self._missing_ciphertext_bytes(backup_id, manifest)
        safety_percent = int(self.contract["capacity_baseline"]["safety_margin_percent"])
        required_with_margin = missing * (100 + safety_percent) // 100
        if shutil.disk_usage(self.state_root).free < required_with_margin:
            raise SafetyError("insufficient encrypted cache space")

    def _target_restore_requirements(self, manifest: dict[str, Any]) -> tuple[int, int]:
        replacement_bytes = 0
        replacement_inodes = 0
        missing_directories: set[Path] = set()
        for item in manifest["files"]:
            relative = Path(str(item["path"]))
            target = self.file_root / relative
            parent = self.file_root
            for part in relative.parts[:-1]:
                parent /= part
                if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                    raise SafetyError("restore target parent is unsafe")
                if not parent.exists():
                    missing_directories.add(parent)
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise SafetyError("restore target is unsafe")
            expected = (int(item["size"]), str(item["sha256"]))
            if target.is_file() and _size_and_sha256(target) == expected:
                continue
            replacement_bytes += expected[0]
            replacement_inodes += 1
        return replacement_bytes, replacement_inodes + len(missing_directories)

    def _reclaimable_restore_temporary_capacity(self, manifest: dict[str, Any]) -> tuple[int, int]:
        """Count stale regular restore temporaries that this locked run will discard."""

        reclaimable_bytes = 0
        reclaimable_inodes = 0
        for item in manifest["files"]:
            target = self.file_root / Path(str(item["path"]))
            temporary = target.with_name(f".{target.name}.restore")
            if temporary.is_symlink() or (temporary.exists() and not temporary.is_file()):
                raise SafetyError("restore temporary is unsafe")
            expected = (int(item["size"]), str(item["sha256"]))
            if target.is_file() and _size_and_sha256(target) == expected:
                continue
            if temporary.is_file():
                reclaimable_bytes += temporary.stat().st_blocks * 512
                reclaimable_inodes += 1
        return reclaimable_bytes, reclaimable_inodes

    def _require_target_capacity(self, manifest: dict[str, Any], *, safety_percent: int) -> None:
        required_bytes, required_inodes = self._target_restore_requirements(manifest)
        reclaimable_bytes, reclaimable_inodes = self._reclaimable_restore_temporary_capacity(
            manifest
        )
        required_with_margin = required_bytes * (100 + safety_percent) // 100
        if shutil.disk_usage(self.file_root).free + reclaimable_bytes < required_with_margin:
            raise SafetyError("insufficient target file space")
        if os.statvfs(self.file_root).f_favail + reclaimable_inodes < required_inodes:
            raise SafetyError("insufficient target inodes")

    def _validate_config_archive(self, ciphertext: Path, manifest: dict[str, Any]) -> None:
        limit = int(self.contract["object_model"]["max_tmpfs_chunk_bytes"])
        archive = decrypt_small(ciphertext, self.identity, limit=limit)
        observed: dict[str, str] = {}
        try:
            with tarfile.open(fileobj=BytesIO(archive), mode="r:*") as bundle:
                for member in bundle.getmembers():
                    name = member.name.removeprefix("./")
                    if member.isdir():
                        continue
                    if not member.isfile() or name.startswith("/") or ".." in Path(name).parts:
                        raise SafetyError("config archive contains an unsafe object")
                    stream = bundle.extractfile(member)
                    if stream is None:
                        raise SafetyError("config archive object cannot be read")
                    observed[name] = hashlib.sha256(stream.read()).hexdigest()
        except tarfile.TarError as error:
            raise SafetyError("config archive is invalid") from error
        if observed != manifest["config_hashes"]:
            raise SafetyError("config archive and manifest hashes differ")

    def _manifest(self, backup_id: str) -> dict[str, Any]:
        try:
            completion = json.loads(self.remote.read_bytes(self._completion_key(backup_id)))
        except (json.JSONDecodeError, OSError) as error:
            raise SafetyError("completion marker is missing or invalid") from error
        if (
            not isinstance(completion, dict)
            or completion.get("backup_id") != backup_id
            or completion.get("status") != "remote_ciphertext_verified"
        ):
            raise SafetyError("completion marker identity or status mismatch")
        cipher = self._download_verified(
            backup_id,
            key=str(completion["manifest_key"]),
            size=int(completion["manifest_ciphertext_size"]),
            digest=str(completion["manifest_ciphertext_sha256"]),
        )
        try:
            manifest = json.loads(decrypt_small(cipher, self.identity))
        except json.JSONDecodeError as error:
            raise SafetyError("authenticated manifest is invalid") from error
        if not isinstance(manifest, dict):
            raise SafetyError("authenticated manifest is not a JSON object")
        verify_manifest_authentication(
            manifest,
            key=self.manifest_verification_key,
            key_id=self.manifest_authentication_key_id,
        )
        validate_manifest(manifest)
        if manifest["backup_id"] != backup_id:
            raise SafetyError("manifest identity mismatch")
        return cast(dict[str, Any], manifest)

    def retain_site(self, backup_id: str) -> None:
        """Create and verify an encrypted on-site rollback set before DB mutation."""

        if self._completed(backup_id, "site_retained"):
            return
        if self._read_state(backup_id)["stage"] != "preflight_complete":
            raise SafetyError("site retention requires completed preflight")
        operation_id = self._operation_id()
        onsite_root = self.state_root / "site-retention" / backup_id / operation_id
        staging_root = onsite_root.with_name(f".{operation_id}.staging")
        for path in (staging_root, onsite_root):
            if path.exists():
                if not path.is_dir() or path.is_symlink():
                    raise SafetyError("uncheckpointed site retention path is unsafe")
                shutil.rmtree(path)
        staging_root.mkdir(parents=True, mode=0o700)
        try:
            self._inject("site_retention")
            onsite_remote = LocalRemote(staging_root)
            cipher = AgeCipher(self.recipient)
            database = cipher.encrypt_command(
                ["pg_dump", "--format=custom", "--no-owner"],
                onsite_remote,
                "database.dump.age",
            )
            file_manifest = canonical_json(
                inventory(self.file_root, excluded_top_level_names={"temporary"})
            )
            file_cipher_size, file_cipher_digest = cipher.encrypt_bytes(
                file_manifest, onsite_remote, "files.json.age"
            )
            deployment = self._current_deployment_identity()
            identity = {
                "environment_id": self.environment_id,
                "target_backup_id": backup_id,
                "restore_operation_id": operation_id,
                "retained_at": format_time(utc_now()),
                "database_plaintext_sha256": database[1],
                "database_ciphertext_sha256": database[3],
                "file_manifest_ciphertext_sha256": file_cipher_digest,
                **deployment,
            }
            identity_size, identity_digest = cipher.encrypt_bytes(
                canonical_json(identity), onsite_remote, "identity.json.age"
            )
            checks = {
                "database": (database[2], database[3]),
                "files": (file_cipher_size, file_cipher_digest),
                "identity": (identity_size, identity_digest),
            }
            for name, expected in checks.items():
                filename = "database.dump.age" if name == "database" else f"{name}.json.age"
                if onsite_remote.size_and_sha256(filename) != expected:
                    raise SafetyError("site retention verification failed")
            staging_root.replace(onsite_root)
        except BaseException:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise
        self._advance(backup_id, "site_retained")

    def _current_config_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for entry in self.contract["recoverable_config_allowlist"]:
            path = self.repository_root / str(entry)
            candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
            for candidate in candidates:
                if candidate.is_dir():
                    continue
                if "__pycache__" in candidate.parts or candidate.suffix in {".pyc", ".pyo"}:
                    continue
                if not candidate.is_file() or candidate.is_symlink():
                    raise SafetyError("current config contains an unsafe object")
                payload = candidate.read_bytes()
                if any(
                    marker in payload
                    for marker in (
                        b"AGE-" + b"SECRET-KEY-",
                        b"-----BEGIN " + b"PRIVATE KEY-----",
                        b"aws_secret_" + b"access_key =",
                    )
                ):
                    raise SafetyError("current config contains secret material")
                hashes[candidate.relative_to(self.repository_root).as_posix()] = hashlib.sha256(
                    payload
                ).hexdigest()
        return hashes

    def _current_deployment_identity(self) -> dict[str, Any]:
        source_commit = os.environ.get("SOURCE_COMMIT", "")
        images = {
            name: os.environ.get(variable, "") for name, variable in IMAGE_ENVIRONMENT.items()
        }
        if (
            len(source_commit) != 40
            or any(character not in "0123456789abcdef" for character in source_commit)
            or any("@sha256:" not in reference for reference in images.values())
        ):
            raise SafetyError("current deployment identity is incomplete")
        return {
            "source_commit": source_commit,
            "images": images,
            "config_hashes": self._current_config_hashes(),
        }

    def _validate_manifest_deployment_identity(self, manifest: dict[str, Any]) -> None:
        current = self._current_deployment_identity()
        if manifest["source_commit"] != current["source_commit"]:
            raise SafetyError("backup source commit differs from target checkout")
        if manifest["images"] != current["images"]:
            raise SafetyError("backup image set differs from target deployment")
        if manifest["config_hashes"] != current["config_hashes"]:
            raise SafetyError("backup config set differs from target checkout")

    def restore_database(self, backup_id: str) -> None:
        """Restore owned schema objects with the one-time app_migrate role."""

        if self._completed(backup_id, "database_restored"):
            return
        if self._read_state(backup_id)["stage"] != "site_retained":
            raise SafetyError("database restore requires verified site retention")
        self._inject("database")
        manifest = self._manifest(backup_id)
        database = next(item for item in manifest["objects"] if item["name"] == "database")
        ciphertext = self._cache_path(backup_id, str(database["key"]))
        decrypt_to_process(
            ciphertext,
            self.identity,
            [
                "pg_restore",
                "--exit-on-error",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--role=app_migrate",
                "--dbname",
                os.environ.get("PGDATABASE", ""),
            ],
        )
        self._advance(backup_id, "database_restored")

    def restore_files(self, backup_id: str) -> None:
        """Restore only referenced objects and preserve post-backup append-only files."""

        if self._completed(backup_id, "files_complete"):
            return
        if self._read_state(backup_id)["stage"] != "database_restored":
            raise SafetyError("file restore requires database_restored")
        self._inject("files")
        manifest = self._manifest(backup_id)
        object_by_name = {str(item["name"]): item for item in manifest["objects"]}
        for file_item in manifest["files"]:
            self._inject("files")
            target = self.file_root / str(file_item["path"])
            if target.is_file() and _size_and_sha256(target) == (
                int(file_item["size"]),
                str(file_item["sha256"]),
            ):
                continue
            obj = object_by_name[f"file:{file_item['path']}"]
            ciphertext = self._cache_path(backup_id, str(obj["key"]))
            observed = decrypt_to_path(ciphertext, self.identity, target)
            if observed != (int(file_item["size"]), str(file_item["sha256"])):
                raise SafetyError("restored file digest mismatch")
        self._advance(backup_id, "files_complete")

    def offline_validate(self, backup_id: str) -> dict[str, Any]:
        """Validate all current/history file references and DB/audit projections."""

        already_complete = self._completed(backup_id, "offline_validated")
        if not already_complete and self._read_state(backup_id)["stage"] != "files_complete":
            raise SafetyError("offline validation requires files_complete")
        self._inject("offline_validation")
        manifest = self._manifest(backup_id)
        for item in manifest["files"]:
            path = self.file_root / str(item["path"])
            if not path.is_file() or _size_and_sha256(path) != (
                int(item["size"]),
                str(item["sha256"]),
            ):
                raise SafetyError("full file validation failed")
        assertions = _database_assertions()
        if assertions != manifest["database_assertions"]:
            raise SafetyError("database counts, relations, or audit projection changed")
        _validate_database_file_references(
            manifest,
            _database_file_references(),
        )
        references = _psql_scalar(
            "SELECT count(*) FROM pdf_versions v JOIN pdf_files f ON f.id=v.pdf_file_id "
            "JOIN products p ON p.id=v.product_id "
            "WHERE f.storage_path IS NULL OR f.sha256 IS NULL "
            "OR (p.current_version_id=v.id AND v.product_id<>p.id);"
        )
        if references != "0":
            raise SafetyError("database contains an invalid current/history reference")
        if not already_complete:
            self._advance(backup_id, "offline_validated")
        return {
            "backup_id": backup_id,
            "stage": "offline_validated",
            "file_count": len(manifest["files"]),
            "table_counts": assertions["table_counts"],
        }

    def record_functional_validation(self, backup_id: str, evidence: Path) -> None:
        """Consume isolated app evidence; no public proxy may be open yet."""

        already_complete = self._completed(backup_id, "isolated_functional_validated")
        if not already_complete and self._read_state(backup_id)["stage"] != "offline_validated":
            raise SafetyError("functional validation requires offline_validated")
        self._inject("isolated_functional_validation")
        try:
            report = json.loads(evidence.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SafetyError("functional evidence is invalid") from error
        required = {
            "unuploaded_state",
            "active_current_v2",
            "disabled_state_priority",
            "switch_v1_and_v2",
            "disabled_immediate_no_store",
            "audit_append_only",
            "proxy_stopped",
            "public_unreachable",
        }
        if report.get("backup_id") != backup_id or any(
            report.get(key) is not True for key in required
        ):
            raise SafetyError("isolated functional validation is incomplete")
        if not already_complete:
            self._advance(
                backup_id,
                "isolated_functional_validated",
                evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
            )

    def authorize_proxy(self, backup_id: str) -> None:
        """Expose the only gate that permits the host to start proxy last."""

        if self._completed(backup_id, "proxy_authorized"):
            return
        if self._read_state(backup_id)["stage"] != "isolated_functional_validated":
            raise SafetyError("proxy remains blocked until isolated validation passes")
        self._inject("proxy")
        self._advance(backup_id, "proxy_authorized")

    def external_ready(self, backup_id: str) -> dict[str, Any]:
        """Record RTO completion only after the host proves public readiness."""

        already_complete = self._completed(backup_id, "external_ready")
        if not already_complete and self._read_state(backup_id)["stage"] != "proxy_authorized":
            raise SafetyError("external readiness reported before proxy authorization")
        state = self._read_state(backup_id)
        if already_complete:
            try:
                elapsed = next(
                    int(item["elapsed_seconds"])
                    for item in reversed(state["history"])
                    if item["stage"] == "external_ready"
                )
            except (KeyError, StopIteration, TypeError, ValueError) as error:
                raise SafetyError("restore completion RTO evidence is invalid") from error
        else:
            elapsed = int((utc_now() - self._declared_at(state)).total_seconds())
            if elapsed < 0:
                raise SafetyError("restore RTO clock moved backwards")
            if elapsed > int(self.contract["capacity_baseline"]["rto_hard_limit_seconds"]):
                raise SafetyError("four-hour restore RTO exceeded")
            self._advance(backup_id, "external_ready", elapsed_seconds=elapsed)
        state = self._read_state(backup_id)
        manifest = self._manifest(backup_id)
        unsigned_verified = {
            "schema_version": 1,
            "backup_id": backup_id,
            "restore_operation_id": self._operation_id(),
            "environment_id": self.environment_id,
            "verified_at": format_time(utc_now()),
            "rule": "remote_download_plus_complete_isolated_restore_only",
            "restore_history_sha256": hashlib.sha256(canonical_json(state["history"])).hexdigest(),
            "manifest_sha256": hashlib.sha256(canonical_json(manifest)).hexdigest(),
        }
        key = f"{self.prefix}/verified/{backup_id}.json"
        public_key = (
            Ed25519PrivateKey.from_private_bytes(self.restore_verification_authentication_key)
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        if self.remote.exists(key):
            existing = json.loads(self.remote.read_bytes(key))
            if not isinstance(existing, dict):
                raise SafetyError("restore verification marker is not a JSON object")
            verify_restore_verification(
                existing,
                key=public_key,
                key_id=self.restore_verification_authentication_key_id,
            )
            if (
                existing.get("backup_id") != backup_id
                or existing.get("manifest_sha256") != unsigned_verified["manifest_sha256"]
            ):
                raise SafetyError("restore verification marker conflicts with this recovery point")
        else:
            verified = authenticate_restore_verification(
                unsigned_verified,
                key=self.restore_verification_authentication_key,
                key_id=self.restore_verification_authentication_key_id,
            )
            self.remote.upload_stream(BytesIO(canonical_json(verified)), key)
        return {"backup_id": backup_id, "elapsed_seconds": elapsed}

    def rollback_site(self, backup_id: str) -> None:
        """Restore the encrypted pre-recovery DB after proving files remain unchanged."""

        state = self._read_state(backup_id)
        if STAGE_ORDER[str(state["stage"])] < STAGE_ORDER["site_retained"]:
            raise SafetyError("rollback requires a verified site retention point")
        onsite_root = self.state_root / "site-retention" / backup_id / self._operation_id()
        database_cipher = onsite_root / "database.dump.age"
        files_cipher = onsite_root / "files.json.age"
        identity_cipher = onsite_root / "identity.json.age"
        for path in (database_cipher, files_cipher, identity_cipher):
            if not path.is_file():
                raise SafetyError("site retention object is missing")
        identity = json.loads(decrypt_small(identity_cipher, self.identity))
        if identity.get("target_backup_id") != backup_id:
            raise SafetyError("site retention identity mismatch")
        if identity.get("restore_operation_id") != self._operation_id():
            raise SafetyError("site retention operation identity mismatch")
        if _size_and_sha256(database_cipher)[1] != identity["database_ciphertext_sha256"]:
            raise SafetyError("site retention database ciphertext changed")
        retained_files = json.loads(decrypt_small(files_cipher, self.identity))
        for item in retained_files:
            path = self.file_root / str(item["path"])
            if not path.is_file() or _size_and_sha256(path) != (
                int(item["size"]),
                str(item["sha256"]),
            ):
                raise SafetyError("site files changed; automatic rollback is unsafe")
        decrypt_to_process(
            database_cipher,
            self.identity,
            [
                "pg_restore",
                "--exit-on-error",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--role=app_migrate",
                "--dbname",
                os.environ.get("PGDATABASE", ""),
            ],
        )
        state["stage"] = "rolled_back"
        state["history"].append({"stage": "rolled_back", "at": format_time(utc_now())})
        atomic_write(self._state_path(backup_id), canonical_json(state))


def _size_and_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _psql_scalar(sql: str) -> str:
    result = subprocess.run(
        ["psql", "--no-psqlrc", "--tuples-only", "--no-align", "--set=ON_ERROR_STOP=1", "-c", sql],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SafetyError("restore database assertion query failed")
    return result.stdout.strip()


def _database_assertions() -> dict[str, Any]:
    tables = ("admins", "products", "pdf_files", "pdf_versions", "admin_sessions", "audit_events")
    counts = {table: int(_psql_scalar(f"SELECT count(*) FROM {table};")) for table in tables}
    audit = _psql_scalar(
        "SELECT coalesce(jsonb_agg(jsonb_build_array("
        "id,occurred_at,actor_type,actor_id,action,target_type,target_id,"
        "product_code,result,request_id,detail) ORDER BY id)::text,'[]') FROM audit_events;"
    )
    relation = _psql_scalar(
        "SELECT coalesce(jsonb_agg(jsonb_build_array("
        "p.id,p.code,p.status,p.current_version_id,v.id,v.product_id,"
        "v.pdf_file_id,v.version_no,f.sha256,f.size_bytes,f.storage_path"
        ") ORDER BY p.id,v.version_no)::text,'[]') "
        "FROM products p LEFT JOIN pdf_versions v ON v.product_id=p.id "
        "LEFT JOIN pdf_files f ON f.id=v.pdf_file_id;"
    )
    return {
        "table_counts": counts,
        "audit_projection_sha256": hashlib.sha256(audit.encode()).hexdigest(),
        "relation_projection_sha256": hashlib.sha256(relation.encode()).hexdigest(),
    }


def _database_file_references() -> list[dict[str, Any]]:
    payload = _psql_scalar(
        "SELECT coalesce(jsonb_agg(jsonb_build_object("
        "'storage_path',q.storage_path,'sha256',q.sha256,'size_bytes',q.size_bytes"
        ") ORDER BY q.storage_path)::text,'[]') FROM ("
        "SELECT DISTINCT f.storage_path,f.sha256,f.size_bytes "
        "FROM pdf_versions v JOIN pdf_files f ON f.id=v.pdf_file_id"
        ") q;"
    )
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as error:
        raise SafetyError("database file reference projection is invalid") from error
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise SafetyError("database file reference projection is invalid")
    return cast(list[dict[str, Any]], result)


def _validate_database_file_references(
    manifest: dict[str, Any],
    references: list[dict[str, Any]],
) -> None:
    """Prove every current/history DB file is present with the same identity."""

    manifest_by_path = {
        str(item["path"]): (int(item["size"]), str(item["sha256"])) for item in manifest["files"]
    }
    seen: set[str] = set()
    for reference in references:
        storage_path = reference.get("storage_path")
        digest = reference.get("sha256")
        size = reference.get("size_bytes")
        if (
            not isinstance(storage_path, str)
            or not storage_path
            or storage_path.startswith("/")
            or ".." in Path(storage_path).parts
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise SafetyError("database contains an invalid PDF file identity")
        expected_storage_path = f"{digest[:2]}/{digest[2:4]}/{digest}.pdf"
        if storage_path != expected_storage_path:
            raise SafetyError("database PDF storage path does not match its SHA-256 identity")
        manifest_path = f"files/{storage_path}"
        if manifest_path in seen:
            raise SafetyError("database contains duplicate PDF file identities")
        seen.add(manifest_path)
        if manifest_by_path.get(manifest_path) != (size, digest):
            raise SafetyError("database-referenced PDF is missing or has a different identity")
