"""Check that the production environment example is the exact consumer union."""

from __future__ import annotations

import re
from pathlib import Path

from product_pdf_qr.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.prod.example"
COMPOSE = ROOT / "compose.prod.yaml"
INTERPOLATION = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def main() -> int:
    """Compare app fields, Compose interpolation, and example assignments."""

    app_fields = {name.upper() for name in Settings.model_fields}
    compose_fields = set(INTERPOLATION.findall(COMPOSE.read_text(encoding="utf-8")))
    expected = app_fields | compose_fields | {"MIGRATION_DATABASE_URL"}

    actual: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = ASSIGNMENT.fullmatch(line)
        if match is not None:
            actual[match.group(1)] = match.group(2)

    missing = expected - actual.keys()
    extra = actual.keys() - expected
    if missing or extra:
        print(
            "production environment contract differs: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )
        return 1

    forbidden_real_values = {
        "example.com",
        "letsencrypt.org",
        "localhost",
        "127.0.0.1",
    }
    joined = "\n".join(actual.values()).lower()
    if any(value in joined for value in forbidden_real_values):
        print("production environment example contains a non-placeholder endpoint")
        return 1
    if not actual["APP_IMAGE"].startswith("registry.invalid/"):
        print("APP_IMAGE example is not an unusable placeholder")
        return 1
    for image_name in ("DB_IMAGE", "PROXY_IMAGE", "CERTBOT_IMAGE"):
        if not actual[image_name].startswith("registry.invalid/"):
            print(f"{image_name} example is not an unusable placeholder")
            return 1
    if not actual["PUBLIC_DOMAIN"].endswith(".invalid"):
        print("PUBLIC_DOMAIN example is not an unusable placeholder")
        return 1
    if any(
        actual[name].startswith("replace-with-") is False
        for name in (
            "POSTGRES_SUPERUSER_PASSWORD",
            "APP_MIGRATE_PASSWORD",
            "APP_RW_PASSWORD",
            "APP_BACKUP_PASSWORD",
        )
    ):
        print("credential examples must be obvious replacement markers")
        return 1

    print("production environment consumer union validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
