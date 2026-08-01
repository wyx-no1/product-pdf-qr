"""Database migration, invariant, trigger, and role acceptance tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql

BUSINESS_TABLES = {
    "admin_sessions",
    "admins",
    "audit_events",
    "pdf_files",
    "pdf_versions",
    "products",
}


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        pytest.fail(f"{name} is required for integration tests")
    return value


@contextmanager
def migration_environment(url: str) -> Iterator[None]:
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def alembic_config() -> Config:
    return Config("alembic.ini")


def database_name(url: str) -> str:
    normalized_url = url.replace("postgresql+psycopg://", "postgresql://")
    return urlsplit(normalized_url).path.lstrip("/")


def assert_isolated_test_database(migration_url: str, runtime_url: str) -> None:
    migration_database = database_name(migration_url)
    runtime_database = database_name(runtime_url)
    if migration_database != runtime_database or not migration_database.endswith("_test"):
        pytest.fail(
            "Integration migration tests require matching TEST URLs whose "
            "database name ends with '_test'"
        )


def table_names(connection: psycopg.Connection[tuple[object, ...]]) -> set[str]:
    rows = connection.execute(
        """
        SELECT tablename
        FROM pg_catalog.pg_tables
        WHERE schemaname = 'public'
          AND tablename <> 'alembic_version'
        """
    ).fetchall()
    return {str(row[0]) for row in rows}


@pytest.mark.integration
def test_migration_lifecycle_schema_and_permissions() -> None:
    migration_url = required_environment("TEST_MIGRATION_DATABASE_URL")
    runtime_url = required_environment("TEST_DATABASE_URL")
    backup_url = required_environment("TEST_BACKUP_DATABASE_URL")
    assert_isolated_test_database(migration_url, runtime_url)
    assert_isolated_test_database(migration_url, backup_url)
    psycopg_migration_url = migration_url.replace("postgresql+psycopg://", "postgresql://")
    config = alembic_config()

    with migration_environment(migration_url):
        command.upgrade(config, "head")
        with psycopg.connect(psycopg_migration_url) as connection:
            assert table_names(connection) == BUSINESS_TABLES

        command.downgrade(config, "base")
        with psycopg.connect(psycopg_migration_url) as connection:
            assert table_names(connection) == set()
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public AUTHORIZATION app_migrate")
            connection.commit()

        command.upgrade(config, "head")

    with psycopg.connect(psycopg_migration_url, autocommit=True) as connection:
        assert connection.execute("SELECT current_user").fetchone() == ("app_migrate",)
        tables = table_names(connection)
        assert tables == BUSINESS_TABLES

        composite_fk = connection.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conname = 'fk_products_current_version'
            """
        ).fetchone()
        assert composite_fk is not None
        assert (
            "FOREIGN KEY (id, current_version_id) REFERENCES pdf_versions(product_id, id)"
        ) in str(composite_fk[0])

        runtime_table_grants = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                """
                SELECT table_name, privilege_type
                FROM information_schema.table_privileges
                WHERE grantee = 'app_rw'
                  AND table_schema = 'public'
                """
            ).fetchall()
        }
        assert {
            ("pdf_files", "SELECT"),
            ("pdf_files", "INSERT"),
            ("pdf_versions", "SELECT"),
            ("pdf_versions", "INSERT"),
        }.issubset(runtime_table_grants)
        assert ("pdf_files", "UPDATE") not in runtime_table_grants
        assert ("pdf_files", "DELETE") not in runtime_table_grants
        assert ("pdf_versions", "UPDATE") not in runtime_table_grants
        assert ("pdf_versions", "DELETE") not in runtime_table_grants
        assert ("admin_sessions", "DELETE") in runtime_table_grants

        product_update_columns = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT column_name
                FROM information_schema.column_privileges
                WHERE grantee = 'app_rw'
                  AND table_schema = 'public'
                  AND table_name = 'products'
                  AND privilege_type = 'UPDATE'
                """
            ).fetchall()
        }
        assert product_update_columns == {"status", "current_version_id", "updated_at"}

        admin_id = connection.execute(
            """
            INSERT INTO admins (
                username,
                password_hash,
                password_updated_at,
                created_at
            ) VALUES ('synthetic-admin', 'synthetic-hash', now(), now())
            RETURNING id
            """
        ).fetchone()
        assert admin_id is not None
        product_id = connection.execute(
            """
            INSERT INTO products (
                code,
                public_token,
                created_at,
                updated_at
            ) VALUES ('SYNTHETIC-1', 'AAAAAAAAAAAAAAAAAAAAAAAAAA', now(), now())
            RETURNING id
            """
        ).fetchone()
        assert product_id is not None
        file_id = connection.execute(
            """
            INSERT INTO pdf_files (
                sha256,
                size_bytes,
                storage_path,
                created_at
            ) VALUES (%s, 128, %s, now())
            RETURNING id
            """,
            ("a" * 64, f"aa/aa/{'a' * 64}.pdf"),
        ).fetchone()
        assert file_id is not None
        version_id = connection.execute(
            """
            INSERT INTO pdf_versions (
                product_id,
                pdf_file_id,
                version_no,
                original_filename,
                uploaded_by,
                uploaded_at
            ) VALUES (%s, %s, 1, 'synthetic.pdf', %s, now())
            RETURNING id
            """,
            (product_id[0], file_id[0], admin_id[0]),
        ).fetchone()
        assert version_id is not None
        connection.execute(
            "UPDATE products SET current_version_id = %s, updated_at = now() WHERE id = %s",
            (version_id[0], product_id[0]),
        )

        for statement in (
            sql.SQL("UPDATE pdf_files SET size_bytes = size_bytes + 1"),
            sql.SQL("DELETE FROM pdf_files"),
            sql.SQL("UPDATE pdf_versions SET original_filename = 'changed.pdf'"),
            sql.SQL("DELETE FROM pdf_versions"),
        ):
            with pytest.raises(psycopg.errors.RaiseException):
                connection.execute(statement)

        for column in ("code", "public_token", "id"):
            query = sql.SQL("UPDATE products SET {} = {} WHERE id = %s").format(
                sql.Identifier(column),
                sql.Literal("CHANGED") if column != "id" else sql.SQL("id + 100"),
            )
            with pytest.raises(psycopg.errors.RaiseException):
                connection.execute(query, (product_id[0],))

        other_product = connection.execute(
            """
            INSERT INTO products (
                code,
                public_token,
                created_at,
                updated_at
            ) VALUES ('SYNTHETIC-2', 'BBBBBBBBBBBBBBBBBBBBBBBBBB', now(), now())
            RETURNING id
            """
        ).fetchone()
        assert other_product is not None
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                """
                UPDATE products
                SET current_version_id = %s, updated_at = now()
                WHERE id = %s
                """,
                (version_id[0], other_product[0]),
            )

    with psycopg.connect(runtime_url) as runtime_connection:
        assert runtime_connection.execute("SELECT current_user").fetchone() == ("app_rw",)
        runtime_connection.execute(
            """
            INSERT INTO products (
                code,
                public_token,
                created_at,
                updated_at
            ) VALUES ('SYNTHETIC-RUNTIME', 'CCCCCCCCCCCCCCCCCCCCCCCCCC', now(), now())
            """
        )
        runtime_connection.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime_connection.execute("DROP TABLE products")

    with psycopg.connect(backup_url) as backup_connection:
        assert backup_connection.execute("SELECT current_user").fetchone() == ("app_backup",)
        assert backup_connection.execute("SELECT count(*) FROM products").fetchone() is not None
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            backup_connection.execute(
                """
                INSERT INTO products (
                    code,
                    public_token,
                    created_at,
                    updated_at
                ) VALUES ('BACKUP-MUST-FAIL', 'DDDDDDDDDDDDDDDDDDDDDDDDDD', now(), now())
                """
            )
