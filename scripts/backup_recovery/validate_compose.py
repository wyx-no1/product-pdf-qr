"""Validate the PR2A overlay without relaxing the PR1 five-service contract."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

BASE_SERVICES = {"proxy", "certbot", "app", "db", "migrate"}
PR2A_SERVICES = {"backup", "restore"}
INIT_SERVICE = "backup-file-access-init"
IMAGE = re.compile(r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$", re.ASCII)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _mounts(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(mount["target"]): mount for mount in service.get("volumes") or []}


def validate(document: dict[str, Any]) -> None:
    """Assert one-shot profiles, roles, mounts, and runtime hardening."""

    services = document.get("services") or {}
    require(
        set(services) == BASE_SERVICES | PR2A_SERVICES | {INIT_SERVICE},
        "overlay service set changed",
    )
    for name in PR2A_SERVICES:
        service = services[name]
        require(
            IMAGE.fullmatch(str(service.get("image") or "")) is not None,
            f"{name} image mutable",
        )
        require(service.get("user") == "10002:10002", f"{name} user changed")
        require(service.get("read_only") is True, f"{name} root is writable")
        require(service.get("privileged") is not True, f"{name} is privileged")
        require("ALL" in (service.get("cap_drop") or []), f"{name} does not drop ALL")
        require(not service.get("cap_add"), f"{name} adds a capability")
        require(
            "no-new-privileges:true" in (service.get("security_opt") or []),
            f"{name} lacks no-new-privileges",
        )
        require(service.get("restart") == "no", f"{name} is not one-shot")
        require(not service.get("ports"), f"{name} publishes a port")
        require(service.get("network_mode") != "host", f"{name} uses host network")
        require(service.get("pid") != "host", f"{name} uses host PID")
        require(service.get("ipc") != "host", f"{name} uses host IPC")
        require(not service.get("devices"), f"{name} maps devices")
        require(set(service.get("profiles") or []) == {name}, f"{name} profile changed")
        for mount in service.get("volumes") or []:
            require(
                "/var/run/docker.sock"
                not in {str(mount.get("source") or ""), str(mount.get("target") or "")},
                f"{name} mounts Docker socket",
            )
    backup = services["backup"]
    restore = services["restore"]
    backup_mounts = _mounts(backup)
    restore_mounts = _mounts(restore)
    require(backup_mounts["/data/files"].get("read_only") is True, "backup file volume is writable")
    require(
        restore_mounts["/data/files"].get("read_only") is not True,
        "restore file volume is read-only",
    )
    require(set(backup.get("networks") or {}) == {"database"}, "backup network changed")
    require(
        set(restore.get("networks") or {}) == {"database", "frontend"},
        "restore isolation networks changed",
    )
    require(backup["environment"].get("PGUSER") == "app_backup", "backup role changed")
    require(restore["environment"].get("PGUSER") == "app_migrate", "restore role changed")
    require(
        "/run/secrets/age-identity.txt" not in backup_mounts,
        "scheduled backup gained the private key",
    )
    require(
        "/run/secrets/manifest-authentication.key" in backup_mounts,
        "scheduled backup lacks manifest authentication authority",
    )
    require(
        "/run/secrets/age-identity.txt" in restore_mounts,
        "one-time restore identity is missing",
    )
    require(
        "/run/secrets/manifest-authentication.key" not in restore_mounts,
        "restore gained the backup generator key path",
    )
    require(
        "/run/secrets/manifest-verification.key" in restore_mounts,
        "restore lacks the manifest public verification key",
    )
    require(
        restore_mounts["/run/secrets/manifest-verification.key"].get("read_only") is True,
        "restore manifest verification key is writable",
    )
    require(
        "/run/secrets/restore-verification-authentication.key" not in backup_mounts,
        "scheduled backup gained restore verification authority",
    )
    require(
        "/run/secrets/restore-verification-authentication.key" in restore_mounts,
        "restore lacks independent verification authority",
    )
    require(
        restore_mounts["/run/secrets/restore-verification-authentication.key"].get("read_only")
        is True,
        "restore verification signing key is writable",
    )
    require(
        restore["environment"].get("RESTORE_MANIFEST_VERIFICATION_KEY")
        == "/run/secrets/manifest-verification.key",
        "restore manifest verification path changed",
    )
    require(
        bool(restore["environment"].get("BACKUP_MANIFEST_AUTHENTICATION_KEY_ID")),
        "restore lacks manifest authentication key id",
    )
    require(
        restore["environment"].get("RESTORE_VERIFICATION_AUTHENTICATION_KEY")
        == "/run/secrets/restore-verification-authentication.key",
        "restore verification signing path changed",
    )
    require(
        bool(restore["environment"].get("RESTORE_VERIFICATION_AUTHENTICATION_KEY_ID")),
        "restore lacks verification authentication key id",
    )
    serialized_backup = json.dumps(backup, sort_keys=True)
    require("RESTORE_CONFIRMATION" not in serialized_backup, "backup gained restore authorization")

    initializer = services[INIT_SERVICE]
    require(
        IMAGE.fullmatch(str(initializer.get("image") or "")) is not None,
        "file ACL initializer image mutable",
    )
    require(initializer.get("user") == "10001:10001", "file ACL initializer owner changed")
    require(initializer.get("read_only") is True, "file ACL initializer root is writable")
    require(initializer.get("privileged") is not True, "file ACL initializer is privileged")
    require("ALL" in (initializer.get("cap_drop") or []), "file ACL initializer does not drop ALL")
    require(not initializer.get("cap_add"), "file ACL initializer adds a capability")
    require(initializer.get("network_mode") == "none", "file ACL initializer gained a network")
    require(
        set(initializer.get("profiles") or []) == {"backup-volume-init"},
        "file ACL initializer profile changed",
    )
    require(
        set(_mounts(initializer)) == {"/data/files"},
        "file ACL initializer mount set changed",
    )
    require(not initializer.get("environment"), "file ACL initializer gained environment values")


def main() -> int:
    try:
        document = json.load(sys.stdin)
        validate(document)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"backup Compose validation failed: {error}", file=sys.stderr)
        return 1
    print("backup Compose isolation validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
