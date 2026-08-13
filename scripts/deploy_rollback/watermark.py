"""Emit the complete relation/file/audit watermark used by PR2B decisions."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.backup_recovery.model import SafetyError, inventory
from scripts.deploy_rollback.model import RollbackSafetyError, canonical_json, validate_watermark

RELATIONS = (
    "admins",
    "products",
    "pdf_files",
    "pdf_versions",
    "admin_sessions",
    "audit_events",
)


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RollbackSafetyError(f"{name} is required")
    return value


def _relation_projection(connection: Any, relation: str) -> str:
    if relation not in RELATIONS:
        raise RollbackSafetyError("watermark relation is outside the fixed allowlist")
    # Every fixed v1 relation has an immutable primary-key id. jsonb key ordering
    # gives a stable complete row representation without emitting row contents.
    query = (
        f"SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY t.id), '[]'::jsonb)::text "
        f"FROM {relation} AS t"
    )
    with connection.cursor() as cursor:
        cursor.execute(query)
        row = cursor.fetchone()
    if row is None or not isinstance(row[0], str):
        raise RollbackSafetyError(f"could not project relation {relation}")
    try:
        projection = json.loads(row[0])
    except json.JSONDecodeError as error:
        raise RollbackSafetyError(f"relation {relation} projection is invalid") from error
    return hashlib.sha256(canonical_json(projection)).hexdigest()


def build_watermark(*, database_url: str, file_root: Path) -> dict[str, Any]:
    """Read a repeatable, read-only snapshot and hash every retained file."""

    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgresql", "postgres"} or parsed.username != "app_backup":
        raise RollbackSafetyError("watermark database URL must use the read-only app_backup role")
    if (
        not file_root.is_absolute()
        or file_root == Path("/")
        or not file_root.is_dir()
        or file_root.is_symlink()
    ):
        raise RollbackSafetyError("watermark file root must be a bounded regular directory")
    try:
        import psycopg

        with psycopg.connect(database_url) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            relations = {
                relation: _relation_projection(connection, relation) for relation in RELATIONS
            }
            audit_projection = relations["audit_events"]
    except psycopg.Error as error:
        raise RollbackSafetyError("could not build read-only database watermark") from error
    try:
        files = inventory(file_root, excluded_top_level_names={"temporary"})
    except SafetyError as error:
        raise RollbackSafetyError("could not build complete file watermark") from error
    watermark = {
        "relations": relations,
        "files": files,
        "audit_projection": audit_projection,
    }
    validate_watermark(watermark)
    return watermark


def build_watermark_from_pg_environment(*, file_root: Path) -> dict[str, Any]:
    """Use PR2A's app_backup psql environment inside its read-only container."""

    if (
        os.environ.get("PGUSER") != "app_backup"
        or not os.environ.get("PGHOST")
        or not os.environ.get("PGDATABASE")
        or not os.environ.get("PGPASSFILE")
    ):
        raise RollbackSafetyError("PR2A app_backup PostgreSQL environment is incomplete")
    if (
        not file_root.is_absolute()
        or file_root == Path("/")
        or not file_root.is_dir()
        or file_root.is_symlink()
    ):
        raise RollbackSafetyError("watermark file root must be a bounded regular directory")
    fields = ", ".join(
        f"'{relation}', (SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY t.id), "
        f"'[]'::jsonb) FROM {relation} AS t)"
        for relation in RELATIONS
    )
    query = (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; "
        f"SELECT jsonb_build_object({fields})::text; "
        "COMMIT;"
    )
    result = subprocess.run(
        [
            "psql",
            "--no-psqlrc",
            "--quiet",
            "--tuples-only",
            "--no-align",
            "--set=ON_ERROR_STOP=1",
            "--command",
            query,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RollbackSafetyError("could not build read-only database watermark")
    document_line = next(
        (line for line in result.stdout.splitlines() if line.startswith("{")),
        "",
    )
    try:
        document = json.loads(document_line)
    except json.JSONDecodeError as error:
        raise RollbackSafetyError("psql watermark projection is invalid") from error
    if not isinstance(document, dict) or set(document) != set(RELATIONS):
        raise RollbackSafetyError("psql watermark relation set is incomplete")
    relations = {
        relation: hashlib.sha256(canonical_json(document[relation])).hexdigest()
        for relation in RELATIONS
    }
    try:
        files = inventory(file_root, excluded_top_level_names={"temporary"})
    except SafetyError as error:
        raise RollbackSafetyError("could not build complete file watermark") from error
    watermark = {
        "relations": relations,
        "files": files,
        "audit_projection": relations["audit_events"],
    }
    validate_watermark(watermark)
    return watermark


def main() -> None:
    try:
        file_root = Path(_required_environment("PR2B_FILE_ROOT"))
        database_url = os.environ.get("PR2B_DATABASE_URL")
        watermark = (
            build_watermark(database_url=database_url, file_root=file_root)
            if database_url
            else build_watermark_from_pg_environment(file_root=file_root)
        )
    except (RollbackSafetyError, OSError) as error:
        print(
            json.dumps({"status": "refused", "error": str(error)}, sort_keys=True), file=sys.stderr
        )
        raise SystemExit(2) from error
    sys.stdout.buffer.write(canonical_json(watermark))


if __name__ == "__main__":
    main()
