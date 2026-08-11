"""Encrypted content-addressed precopy and consistent finalization."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.backup_recovery.crypto import AgeCipher
from scripts.backup_recovery.model import (
    SafetyError,
    atomic_write,
    authenticate_manifest,
    canonical_json,
    format_time,
    generation_tags,
    inventory,
    utc_now,
)
from scripts.backup_recovery.remote import Remote

BACKUP_FAILURE_STAGES = ("files", "dump", "manifest", "encryption", "upload")


def new_backup_id(now: datetime | None = None) -> str:
    """Create a sortable, collision-resistant restore-point identity."""

    timestamp = (now or utc_now()).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex}"


class BackupBuilder:
    """Build logical bundles without persisting a plaintext copy."""

    def __init__(
        self,
        *,
        contract: dict[str, Any],
        remote: Remote,
        source_root: Path,
        repository_root: Path,
        state_root: Path,
        recipient: str,
        recipient_key_id: str,
        manifest_authentication_key: bytes,
        manifest_authentication_key_id: str,
        remote_prefix: str,
    ) -> None:
        if not recipient_key_id or any(char in recipient_key_id for char in "/\\ \t\n"):
            raise SafetyError("invalid recipient key id")
        self.contract = contract
        self.remote = remote
        self.source_root = source_root
        self.repository_root = repository_root
        self.state_root = state_root
        self.recipient_key_id = recipient_key_id
        self.manifest_authentication_key = manifest_authentication_key
        self.manifest_authentication_key_id = manifest_authentication_key_id
        self.prefix = remote_prefix.strip("/")
        if not self.prefix or ".." in Path(self.prefix).parts:
            raise SafetyError("invalid remote prefix")
        self.cipher = AgeCipher(recipient)

    def _key(self, suffix: str) -> str:
        return f"{self.prefix}/{suffix}"

    def _cas_key(self, digest: str) -> str:
        return self._key(f"objects/sha256/{digest}/{self.recipient_key_id}.age")

    def _cas_meta_key(self, digest: str) -> str:
        return self._key(f"objects/sha256/{digest}/{self.recipient_key_id}.json")

    def _cas_stage_root(self, digest: str) -> Path:
        return self.state_root / "cas-publications" / digest / self.recipient_key_id

    def _validate_cas_metadata(
        self,
        metadata: Any,
        *,
        item: dict[str, Any],
        digest: str,
    ) -> dict[str, Any]:
        required = {
            "schema_version",
            "plaintext_size",
            "plaintext_sha256",
            "ciphertext_size",
            "ciphertext_sha256",
            "recipient_key_id",
        }
        if (
            not isinstance(metadata, dict)
            or set(metadata) != required
            or metadata.get("schema_version") != 1
            or metadata.get("plaintext_sha256") != digest
            or metadata.get("plaintext_size") != item["size"]
            or metadata.get("recipient_key_id") != self.recipient_key_id
            or not isinstance(metadata.get("ciphertext_size"), int)
            or isinstance(metadata.get("ciphertext_size"), bool)
            or int(metadata["ciphertext_size"]) <= 0
            or not isinstance(metadata.get("ciphertext_sha256"), str)
            or len(str(metadata["ciphertext_sha256"])) != 64
            or any(
                character not in "0123456789abcdef"
                for character in str(metadata["ciphertext_sha256"])
            )
        ):
            raise SafetyError("content-addressed metadata mismatch")
        return metadata

    def _inject(self, stage: str) -> None:
        if stage not in BACKUP_FAILURE_STAGES:
            raise SafetyError("unknown backup failure stage")
        if os.environ.get("BACKUP_FAIL_STAGE") == stage:
            raise SafetyError(f"injected backup failure: {stage}")

    def _publish_file_object(self, item: dict[str, Any]) -> dict[str, Any]:
        digest = str(item["sha256"])
        key = self._cas_key(digest)
        meta_key = self._cas_meta_key(digest)
        age_exists = self.remote.exists(key)
        meta_exists = self.remote.exists(meta_key)
        if age_exists and meta_exists:
            metadata = self._validate_cas_metadata(
                json.loads(self.remote.read_bytes(meta_key)),
                item=item,
                digest=digest,
            )
            if self.remote.size_and_sha256(key) != (
                metadata["ciphertext_size"],
                metadata["ciphertext_sha256"],
            ):
                raise SafetyError("content-addressed metadata mismatch")
            stage_root = self._cas_stage_root(digest)
            if stage_root.exists():
                if not stage_root.is_dir() or stage_root.is_symlink():
                    raise SafetyError("unsafe completed CAS publication checkpoint")
                shutil.rmtree(stage_root)
            return {
                "name": f"file:{item['path']}",
                "key": key,
                "plaintext_size": item["size"],
                "plaintext_sha256": digest,
                "ciphertext_size": metadata["ciphertext_size"],
                "ciphertext_sha256": metadata["ciphertext_sha256"],
            }

        source = self.source_root / str(item["path"])
        stage_root = self._cas_stage_root(digest)
        staged_ciphertext = stage_root / "ciphertext.age"
        staged_metadata = stage_root / "metadata.json"
        stage_root.mkdir(parents=True, exist_ok=True)
        for pattern in (".ciphertext.age.*.encrypting", ".metadata.json.*.tmp"):
            for residue in stage_root.glob(pattern):
                if not residue.is_file() or residue.is_symlink():
                    raise SafetyError("unsafe CAS publication temporary residue")
                residue.unlink()
        if staged_ciphertext.is_file() and staged_metadata.is_file():
            metadata = self._validate_cas_metadata(
                json.loads(staged_metadata.read_text(encoding="utf-8")),
                item=item,
                digest=digest,
            )
            if _size_and_sha256(staged_ciphertext) != (
                metadata["ciphertext_size"],
                metadata["ciphertext_sha256"],
            ):
                raise SafetyError("local CAS publication checkpoint mismatch")
        elif staged_ciphertext.is_file() and not staged_metadata.exists():
            plain_size, plain_digest = _size_and_sha256(source)
            if (plain_size, plain_digest) != (item["size"], digest):
                raise SafetyError("source changed while recovering CAS publication checkpoint")
            cipher_size, cipher_digest = _size_and_sha256(staged_ciphertext)
            metadata = {
                "schema_version": 1,
                "plaintext_size": plain_size,
                "plaintext_sha256": plain_digest,
                "ciphertext_size": cipher_size,
                "ciphertext_sha256": cipher_digest,
                "recipient_key_id": self.recipient_key_id,
            }
            atomic_write(staged_metadata, canonical_json(metadata))
        else:
            if staged_ciphertext.exists() or staged_metadata.exists():
                raise SafetyError("local CAS publication checkpoint is incomplete")
            plain_size, plain_digest, cipher_size, cipher_digest = self.cipher.encrypt_file_to_path(
                source, staged_ciphertext
            )
            if (plain_size, plain_digest) != (item["size"], digest):
                shutil.rmtree(stage_root, ignore_errors=True)
                raise SafetyError("source changed between inventory and encryption")
            metadata = {
                "schema_version": 1,
                "plaintext_size": plain_size,
                "plaintext_sha256": plain_digest,
                "ciphertext_size": cipher_size,
                "ciphertext_sha256": cipher_digest,
                "recipient_key_id": self.recipient_key_id,
            }
            atomic_write(staged_metadata, canonical_json(metadata))

        expected_ciphertext = (
            int(metadata["ciphertext_size"]),
            str(metadata["ciphertext_sha256"]),
        )
        if age_exists:
            if self.remote.size_and_sha256(key) != expected_ciphertext:
                raise SafetyError("remote CAS ciphertext conflicts with local checkpoint")
        else:
            self.remote.upload_file(staged_ciphertext, key)
            if self.remote.size_and_sha256(key) != expected_ciphertext:
                raise SafetyError("remote file ciphertext verification failed")
        expected_metadata = canonical_json(metadata)
        if meta_exists:
            if self.remote.read_bytes(meta_key) != expected_metadata:
                raise SafetyError("remote CAS metadata conflicts with local checkpoint")
        else:
            self.remote.upload_stream(source=_bytes_stream(expected_metadata), key=meta_key)
            if self.remote.read_bytes(meta_key) != expected_metadata:
                raise SafetyError("remote CAS metadata verification failed")
        shutil.rmtree(stage_root, ignore_errors=True)
        return {
            "name": f"file:{item['path']}",
            "key": key,
            **{key: value for key, value in metadata.items() if key != "schema_version"},
        }

    def precopy(self) -> dict[str, Any]:
        """Read the live source only and encrypt missing SHA-addressed objects."""

        started = utc_now()
        self._inject("files")
        files = inventory(self.source_root, excluded_top_level_names={"temporary"})
        published = 0
        reused = 0
        for item in files:
            existed = self.remote.exists(self._cas_key(str(item["sha256"])))
            self._publish_file_object(item)
            reused += int(existed)
            published += int(not existed)
        result = {
            "status": "precopy_only_not_a_restore_point",
            "started_at": format_time(started),
            "completed_at": format_time(utc_now()),
            "file_count": len(files),
            "file_bytes": sum(int(item["size"]) for item in files),
            "published": published,
            "reused": reused,
        }
        atomic_write(self.state_root / "last-precopy.json", canonical_json(result))
        return result

    def _psql_scalar(self, sql: str) -> str:
        result = subprocess.run(
            [
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--set=ON_ERROR_STOP=1",
                "-c",
                sql,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise SafetyError("database identity query failed")
        return result.stdout.strip()

    def assert_quiescent(self) -> None:
        """Prove no application writer or migration session remains."""

        count = self._psql_scalar(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE pid <> pg_backend_pid() "
            "AND (usename IN ('app_rw','app_migrate') "
            "OR (backend_xid IS NOT NULL AND usename <> 'app_backup'));"
        )
        if count != "0":
            raise SafetyError("database has an application writer or write transaction")

    def _database_assertions(self) -> dict[str, Any]:
        tables = (
            "admins",
            "products",
            "pdf_files",
            "pdf_versions",
            "admin_sessions",
            "audit_events",
        )
        counts = {
            table: int(self._psql_scalar(f"SELECT count(*) FROM {table};")) for table in tables
        }
        audit_projection = self._psql_scalar(
            "SELECT coalesce(jsonb_agg(jsonb_build_array("
            "id,occurred_at,actor_type,actor_id,action,target_type,target_id,"
            "product_code,result,request_id,detail) ORDER BY id)::text,'[]') FROM audit_events;"
        )
        relation_projection = self._psql_scalar(
            "SELECT coalesce(jsonb_agg(jsonb_build_array("
            "p.id,p.code,p.status,p.current_version_id,v.id,v.product_id,"
            "v.pdf_file_id,v.version_no,f.sha256,f.size_bytes,f.storage_path"
            ") ORDER BY p.id,v.version_no)::text,'[]') "
            "FROM products p LEFT JOIN pdf_versions v ON v.product_id=p.id "
            "LEFT JOIN pdf_files f ON f.id=v.pdf_file_id;"
        )
        return {
            "table_counts": counts,
            "audit_projection_sha256": hashlib.sha256(audit_projection.encode()).hexdigest(),
            "relation_projection_sha256": hashlib.sha256(relation_projection.encode()).hexdigest(),
        }

    def _config_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for entry in self.contract["recoverable_config_allowlist"]:
            path = self.repository_root / entry
            candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
            for candidate in candidates:
                if candidate.is_dir():
                    continue
                if "__pycache__" in candidate.parts or candidate.suffix in {".pyc", ".pyo"}:
                    continue
                if not candidate.is_file() or candidate.is_symlink():
                    raise SafetyError(f"invalid recoverable config object: {candidate}")
                relative = candidate.relative_to(self.repository_root).as_posix()
                payload = candidate.read_bytes()
                if any(
                    marker in payload
                    for marker in (
                        b"AGE-" + b"SECRET-KEY-",
                        b"-----BEGIN " + b"PRIVATE KEY-----",
                        b"aws_secret_" + b"access_key =",
                    )
                ):
                    raise SafetyError("recoverable config contains secret material")
                digest = hashlib.sha256(payload).hexdigest()
                hashes[relative] = digest
        if not hashes:
            raise SafetyError("recoverable configuration is empty")
        return hashes

    def finalize(self) -> dict[str, Any]:
        """Freeze files first, dump DB second, encrypt config, and publish marker last."""

        started = utc_now()
        backup_id = new_backup_id(started)
        self.assert_quiescent()
        self._inject("files")
        files = inventory(self.source_root, excluded_top_level_names={"temporary"})
        file_objects = []
        for item in files:
            self.assert_quiescent()
            file_objects.append(self._publish_file_object(item))
        frozen = utc_now()

        self._inject("dump")
        self.assert_quiescent()
        database_key = self._key(f"points/{backup_id}/database.dump.age")
        database = self.cipher.encrypt_command(
            ["pg_dump", "--format=custom", "--no-owner"],
            self.remote,
            database_key,
        )
        if self.remote.size_and_sha256(database_key) != database[2:]:
            raise SafetyError("remote database ciphertext verification failed")
        self.assert_quiescent()
        assertions = self._database_assertions()

        self._inject("manifest")
        config_hashes = self._config_hashes()
        config_key = self._key(f"points/{backup_id}/config.tar.age")
        allowlist = sorted(config_hashes)
        config = self.cipher.encrypt_command(
            [
                "tar",
                "--create",
                "--file",
                "-",
                "--directory",
                str(self.repository_root),
                *allowlist,
            ],
            self.remote,
            config_key,
        )
        if self.remote.size_and_sha256(config_key) != config[2:]:
            raise SafetyError("remote config ciphertext verification failed")

        tools = {
            "postgresql": self._psql_scalar("SHOW server_version;"),
            "pg_dump": _tool_version(["pg_dump", "--version"]),
            "age": self.cipher.version(),
            "rclone": _tool_version(["rclone", "version"]),
        }
        alembic_revision = self._psql_scalar("SELECT version_num FROM alembic_version;")
        source_commit = os.environ.get("SOURCE_COMMIT", "")
        if len(source_commit) != 40:
            raise SafetyError("SOURCE_COMMIT must be a full Git SHA")
        objects = [
            {
                "name": "database",
                "backup_id": backup_id,
                "key": database_key,
                "plaintext_size": database[0],
                "plaintext_sha256": database[1],
                "ciphertext_size": database[2],
                "ciphertext_sha256": database[3],
            },
            {
                "name": "config",
                "backup_id": backup_id,
                "key": config_key,
                "plaintext_size": config[0],
                "plaintext_sha256": config[1],
                "ciphertext_size": config[2],
                "ciphertext_sha256": config[3],
            },
        ]
        objects.extend({"backup_id": backup_id, **item} for item in file_objects)
        manifest = authenticate_manifest(
            {
                "schema_version": 1,
                "backup_id": backup_id,
                "started_at": format_time(started),
                "frozen_at": format_time(frozen),
                "completed_at": format_time(utc_now()),
                "source_commit": source_commit,
                "images": {
                    name: os.environ.get(variable, "")
                    for name, variable in {
                        "app": "APP_IMAGE",
                        "db": "DB_IMAGE",
                        "proxy": "PROXY_IMAGE",
                        "certbot": "CERTBOT_IMAGE",
                        "backup_recovery": "BACKUP_IMAGE",
                    }.items()
                },
                "config_hashes": config_hashes,
                "alembic_revision": alembic_revision,
                "tools": tools,
                "volume_name": "product_pdf_qr_files",
                "database_name": os.environ.get("PGDATABASE", ""),
                "recipient_key_id": self.recipient_key_id,
                "database_assertions": assertions,
                "files": files,
                "objects": objects,
                "generations": sorted(generation_tags(frozen, self.contract["business_timezone"])),
            },
            key=self.manifest_authentication_key,
            key_id=self.manifest_authentication_key_id,
        )
        self._inject("encryption")
        manifest_key = self._key(f"points/{backup_id}/manifest.json.age")
        manifest_cipher_size, manifest_cipher_digest = self.cipher.encrypt_bytes(
            canonical_json(manifest), self.remote, manifest_key
        )
        if self.remote.size_and_sha256(manifest_key) != (
            manifest_cipher_size,
            manifest_cipher_digest,
        ):
            raise SafetyError("remote manifest ciphertext verification failed")

        self._inject("upload")
        completion_object_keys = {str(item["key"]) for item in objects}
        completion_object_keys.add(manifest_key)
        completion_object_keys.update(
            str(item["key"]).removesuffix(".age") + ".json"
            for item in objects
            if str(item["name"]).startswith("file:")
        )
        completion = {
            "schema_version": 1,
            "backup_id": backup_id,
            "frozen_at": manifest["frozen_at"],
            "verified_at": format_time(utc_now()),
            "manifest_key": manifest_key,
            "manifest_ciphertext_size": manifest_cipher_size,
            "manifest_ciphertext_sha256": manifest_cipher_digest,
            "recipient_key_id": self.recipient_key_id,
            "generations": manifest["generations"],
            "object_keys": sorted(completion_object_keys),
            "status": "remote_ciphertext_verified",
        }
        completion_key = self._key(f"complete/{backup_id}.json")
        self.remote.upload_stream(_bytes_stream(canonical_json(completion)), completion_key)
        success = {
            "backup_id": backup_id,
            "frozen_at": manifest["frozen_at"],
            "remote_verified_at": completion["verified_at"],
            "completion_key": completion_key,
        }
        atomic_write(self.state_root / "last-success.json", canonical_json(success))
        return success


def _tool_version(command: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise SafetyError(f"tool version failed: {command[0]}")
    return (result.stdout or result.stderr).splitlines()[0].strip()


def _bytes_stream(payload: bytes) -> Any:
    from io import BytesIO

    return BytesIO(payload)


def _size_and_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()
