"""Validate a redacted production Compose JSON stream without echoing its values."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

EXPECTED_SERVICES = {"proxy", "certbot", "app", "db", "migrate"}
EXPECTED_NETWORKS = {
    "proxy": {"frontend"},
    "certbot": {"acme_egress"},
    "app": {"frontend", "database"},
    "db": {"database"},
    "migrate": {"database"},
}
EXPECTED_VOLUMES = {
    ("app", "file_data", "/data/files", False),
    ("app", "app_logs", "/var/log/product-pdf-qr", False),
    ("db", "postgres_data", "/var/lib/postgresql/data", False),
    ("proxy", "certificates", "/etc/certbot", True),
    ("proxy", "acme_challenge", "/var/www/acme", True),
    ("proxy", "proxy_logs", "/var/cache/nginx", False),
    ("certbot", "certificates", "/tmp", False),
    ("certbot", "acme_challenge", "/var/tmp", False),
}
ALLOWED_BINDS = {
    ("proxy", "/etc/nginx/nginx.conf"),
    ("proxy", "/etc/nginx/templates"),
    ("db", "/docker-entrypoint-initdb.d"),
    ("db", "/usr/local/bin/app-db-healthcheck"),
}
IMAGE_PATTERN = re.compile(
    r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$",
    re.ASCII,
)


def require(condition: bool, message: str) -> None:
    """Stop on the first structural contract violation."""

    if not condition:
        raise ValueError(message)


def _network_names(service: dict[str, Any]) -> set[str]:
    networks = service.get("networks") or {}
    return set(networks)


def validate(document: dict[str, Any]) -> None:
    """Apply T34-01 through T34-04 static topology assertions."""

    services = document.get("services") or {}
    require(set(services) == EXPECTED_SERVICES, "production service set changed")
    require(
        set(document.get("networks") or {}) == {"frontend", "database", "acme_egress"},
        "network set changed",
    )

    observed_named_volumes: set[tuple[str, str, str, bool]] = set()
    observed_binds: set[tuple[str, str]] = set()
    for service_name, service in services.items():
        require(not service.get("build"), f"{service_name} must use a prebuilt image")
        image = str(service.get("image") or "")
        require(
            IMAGE_PATTERN.fullmatch(image) is not None, f"{service_name} image is not digest pinned"
        )
        require(":latest@" not in image, f"{service_name} image uses latest")
        require(service.get("privileged") is not True, f"{service_name} is privileged")
        require(service.get("read_only") is True, f"{service_name} root filesystem is writable")
        require(
            "ALL" in (service.get("cap_drop") or []),
            f"{service_name} does not drop all capabilities",
        )
        require(not service.get("cap_add"), f"{service_name} adds capabilities")
        require(service.get("network_mode") != "host", f"{service_name} uses host networking")
        require(service.get("pid") != "host", f"{service_name} shares host PID namespace")
        require(service.get("ipc") != "host", f"{service_name} shares host IPC namespace")
        require(not service.get("devices"), f"{service_name} maps a host device")
        require(
            "no-new-privileges:true" in (service.get("security_opt") or []),
            f"{service_name} lacks no-new-privileges",
        )
        require(
            _network_names(service) == EXPECTED_NETWORKS[service_name],
            f"{service_name} network membership changed",
        )

        ports = service.get("ports") or []
        if service_name == "proxy":
            require(
                {str(port["published"]) for port in ports} == {"80", "443"},
                "proxy published ports changed",
            )
        else:
            require(not ports, f"{service_name} publishes a host port")

        extra_hosts = service.get("extra_hosts") or []
        require(
            not any("host-gateway" in str(item) for item in extra_hosts),
            f"{service_name} uses host-gateway",
        )
        for mount in service.get("volumes") or []:
            source = str(mount.get("source") or "")
            target = str(mount.get("target") or "")
            require(
                "/var/run/docker.sock" not in (source, target),
                f"{service_name} mounts Docker socket",
            )
            if mount.get("type") == "bind":
                observed_binds.add((service_name, target))
                require(mount.get("read_only") is True, f"{service_name} has writable bind mount")
                require(Path(source).is_absolute(), f"{service_name} bind source is not normalized")
            elif mount.get("type") == "volume":
                observed_named_volumes.add(
                    (service_name, source, target, bool(mount.get("read_only")))
                )
            else:
                raise ValueError(f"{service_name} has an unknown mount type")

    require(observed_binds == ALLOWED_BINDS, "bind-mount allowlist changed")
    require(observed_named_volumes == EXPECTED_VOLUMES, "named-volume matrix changed")

    networks = document["networks"]
    require(
        networks["frontend"].get("internal") is not True,
        "frontend network must carry published proxy ports",
    )
    require(networks["database"].get("internal") is True, "database network must be internal")
    require(
        networks["acme_egress"].get("internal") is not True,
        "ACME network must permit outbound ACME",
    )
    require(
        services["app"]["environment"]["FORWARDED_ALLOW_IPS"] == "172.30.0.10",
        "Uvicorn proxy trust changed",
    )
    require(
        services["app"]["environment"]["APP_BIND_HOST"] == "172.30.0.20", "app bind address changed"
    )
    require(services["app"].get("healthcheck") is not None, "app readiness healthcheck is missing")

    volume_names = {
        key: value.get("name") for key, value in (document.get("volumes") or {}).items()
    }
    require(volume_names["file_data"] == "product_pdf_qr_files", "business volume name changed")
    require(
        volume_names["postgres_data"] == "product_pdf_qr_postgres", "database volume name changed"
    )


def main() -> int:
    """Read one ephemeral JSON stream and emit a non-sensitive result only."""

    try:
        document = json.load(sys.stdin)
        validate(document)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"production Compose validation failed: {error}", file=sys.stderr)
        return 1
    print("production Compose topology validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
