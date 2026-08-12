"""Synthetic host boundary used while real PR2B/PR2A shell entrypoints run.

The rehearsal starts a real isolated PostgreSQL service. This module replaces
only Docker service control and external HTTPS, records every requested phase,
and applies deterministic B0/validation database and file changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg

from scripts.deploy_rollback.model import RollbackSafetyError, atomic_write, canonical_json


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RollbackSafetyError(f"{name} is required by the synthetic fixture")
    return value


def _psycopg_url() -> str:
    return _required("PR2B_SYNTHETIC_MIGRATION_URL").replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


def _state_path() -> Path:
    return Path(_required("PR2B_SYNTHETIC_SERVICE_STATE"))


def _state() -> dict[str, Any]:
    value = json.loads(_state_path().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RollbackSafetyError("synthetic service state must be a JSON object")
    return value


def _save(value: dict[str, Any]) -> None:
    atomic_write(_state_path(), canonical_json(value))


def _record_call(value: dict[str, Any], call: str) -> None:
    value["calls"].append(call)
    _save(value)


def _service_from_container(value: str) -> str:
    for service in ("app", "proxy", "db", "certbot"):
        if value.endswith(f"-{service}"):
            return service
    raise RollbackSafetyError("unknown synthetic container")


def _compose_command(arguments: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item in {"--env-file", "-f", "--profile", "--project-name"}:
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        return item, arguments[index + 1 :]
    raise RollbackSafetyError("synthetic compose command is missing")


def _selected_services(arguments: list[str]) -> list[str]:
    return [item for item in arguments if item in {"app", "proxy", "db", "certbot"}]


def _image_from_environment_or_file() -> str:
    if os.environ.get("APP_IMAGE"):
        return str(os.environ["APP_IMAGE"])
    path = Path(_required("PR2B_SYNTHETIC_PRODUCTION_ENV"))
    return next(
        line.split("=", maxsplit=1)[1]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("APP_IMAGE=")
    )


def _reset_database_b0() -> None:
    with psycopg.connect(_psycopg_url()) as connection:
        connection.execute(
            "TRUNCATE audit_events, admin_sessions, pdf_versions, pdf_files, "
            "products, admins RESTART IDENTITY CASCADE"
        )
        connection.execute(
            """
            INSERT INTO admins (
                id, username, password_hash, must_change_password,
                password_updated_at, last_login_at, created_at
            ) VALUES (
                1, 'synthetic-admin', 'synthetic-noncredential-hash', false,
                '2026-08-12T00:00:00Z', NULL, '2026-08-12T00:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO products (
                id, code, public_token, status, current_version_id,
                created_at, updated_at, name
            ) VALUES (
                1, 'B0_PRODUCT', '0123456789ABCDEFGHJKMNPQRS', 'active', NULL,
                '2026-08-12T00:00:00Z', '2026-08-12T00:00:00Z', 'B0'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO audit_events (
                id, occurred_at, actor_type, actor_id, action, target_type,
                target_id, product_code, result, request_id, detail
            ) VALUES (
                1, '2026-08-12T00:00:00Z', 'system', NULL, 'b0_seed',
                'product', 1, 'B0_PRODUCT', 'success', NULL,
                '{"synthetic": true}'::jsonb
            )
            """
        )


def _reset_files_b0() -> None:
    root = Path(_required("PR2B_FILE_ROOT"))
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "b0-history.pdf").write_bytes(b"%PDF-1.4\nsynthetic B0\n")


def _reset_b0() -> None:
    _reset_database_b0()
    _reset_files_b0()


def _apply_validation() -> None:
    with psycopg.connect(_psycopg_url()) as connection:
        connection.execute(
            """
            INSERT INTO products (
                id, code, public_token, status, current_version_id,
                created_at, updated_at, name
            ) VALUES (
                100, 'VALIDATION_PRODUCT', 'ZYXWVUTSRQPONMLKJHGFEDCBA1',
                'active', NULL, '2026-08-12T00:02:00Z',
                '2026-08-12T00:02:00Z', 'validation'
            )
            """
        )
        content = b"%PDF-1.4\nsynthetic validation\n"
        digest = hashlib.sha256(content).hexdigest()
        connection.execute(
            """
            INSERT INTO pdf_files (id, sha256, size_bytes, storage_path, created_at)
            VALUES (100, %s, %s, 'validation-upload.pdf', '2026-08-12T00:02:00Z')
            """,
            (digest, len(content)),
        )
        connection.execute(
            """
            INSERT INTO pdf_versions (
                id, product_id, pdf_file_id, version_no, original_filename,
                uploaded_by, uploaded_at
            ) VALUES (
                100, 100, 100, 1, 'validation.pdf', 1, '2026-08-12T00:02:00Z'
            )
            """
        )
        connection.execute(
            "UPDATE products SET current_version_id = 100, status = 'disabled', "
            "updated_at = '2026-08-12T00:03:00Z' WHERE id = 100"
        )
        connection.execute(
            """
            INSERT INTO admin_sessions (
                id, admin_id, token_hash, issued_at, expires_at, revoked_at
            ) VALUES (
                '00000000-0000-0000-0000-000000000100', 1,
                %s, '2026-08-12T00:02:00Z', '2026-08-12T01:02:00Z',
                '2026-08-12T00:03:00Z'
            )
            """,
            ("a" * 64,),
        )
        connection.execute(
            """
            INSERT INTO audit_events (
                id, occurred_at, actor_type, actor_id, action, target_type,
                target_id, product_code, result, request_id, detail
            ) VALUES (
                100, '2026-08-12T00:03:00Z', 'admin', 1,
                'compatibility_validation', 'product', 100,
                'VALIDATION_PRODUCT', 'success', NULL,
                '{"all_supported_actions": true}'::jsonb
            )
            """
        )
    Path(_required("PR2B_FILE_ROOT"), "validation-upload.pdf").write_bytes(content)


def docker(arguments: list[str]) -> int:
    value = _state()
    if arguments[:2] == ["image", "inspect"]:
        reference = arguments[-1]
        image_id = value["image_ids"].get(reference)
        if image_id is None:
            return 1
        print(f"sha256:{image_id}")
        return 0
    if arguments and arguments[0] == "inspect":
        container = arguments[-1]
        service = _service_from_container(container)
        if "--format" not in arguments:
            return 0
        format_value = arguments[arguments.index("--format") + 1]
        if "State.Running" in format_value:
            print("true" if value["services"][service]["running"] else "false")
        elif "State.Health" in format_value:
            print("healthy" if value["services"][service]["running"] else "")
        return 0
    if arguments and arguments[0] == "run":
        sys.stdin.read()
        return 0
    if not arguments or arguments[0] != "compose":
        return 0
    command, remainder = _compose_command(arguments[1:])
    _record_call(value, f"compose:{command}:{' '.join(remainder)}")
    value = _state()
    if command == "config":
        print("{}")
    elif command == "stop":
        for service in _selected_services(remainder):
            value["services"][service]["running"] = False
        _save(value)
    elif command in {"up", "create"}:
        for service in _selected_services(remainder):
            value["services"][service]["running"] = command == "up"
            if service == "app":
                value["services"]["app"]["image"] = _image_from_environment_or_file()
        _save(value)
    elif command == "start":
        for service in _selected_services(remainder):
            value["services"][service]["running"] = True
        _save(value)
    elif command == "ps":
        service = next((item for item in reversed(remainder) if item in value["services"]), "")
        if service and ("-q" in remainder or "--quiet" in remainder):
            if "--status" not in remainder or value["services"][service]["running"]:
                print(f"synthetic-{service}")
    elif command == "run" and "restore" in remainder:
        restore_index = remainder.index("restore")
        phase = remainder[restore_index + 1] if len(remainder) > restore_index + 1 else ""
        if phase == "restore-database":
            _reset_database_b0()
        elif phase == "restore-files":
            _reset_files_b0()
        elif phase == "external-ready":
            print('{"elapsed_seconds":1}')
    return 0


def assert_ready() -> None:
    value = _state()
    expected = _required("PR2B_SYNTHETIC_EXPECTED_APP_IMAGE")
    if (
        value["services"]["app"]["image"] != expected
        or value["services"]["app"]["running"] is not True
        or value["services"]["proxy"]["running"] is not True
    ):
        raise RollbackSafetyError("synthetic exact app/proxy readiness failed")


def _publish_candidate() -> None:
    value = _state()
    value["services"]["app"]["image"] = _required("PR2B_SYNTHETIC_CANDIDATE_APP_IMAGE")
    value["services"]["app"]["running"] = True
    value["services"]["proxy"]["running"] = True
    _record_call(value, "publication:candidate-proxy-public")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "docker":
        raise SystemExit(docker(sys.argv[2:]))
    if command == "apply-validation":
        _apply_validation()
        return
    if command == "reset-b0":
        _reset_b0()
        return
    if command in {"noop", "candidate-validation"}:
        return
    if command == "assert-ready":
        assert_ready()
        return
    if command == "publish-candidate":
        _publish_candidate()
        return
    if command == "curl":
        sys.stdout.write(_required("PR2B_SYNTHETIC_EXTERNAL_BODY"))
        return
    raise SystemExit(f"unknown synthetic fixture command: {command}")


if __name__ == "__main__":
    main()
