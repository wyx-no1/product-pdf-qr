"""Create the Phase 1-A database schema and least-privilege grants.

Revision ID: 20260801_0001
Revises:
Create Date: 2026-08-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260801_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create only the schema defined by data-model-v1.md."""

    op.execute(
        """
        CREATE TABLE admins (
            id bigserial PRIMARY KEY,
            username varchar(64) NOT NULL UNIQUE,
            password_hash text NOT NULL,
            must_change_password boolean NOT NULL DEFAULT true,
            password_updated_at timestamptz NOT NULL,
            last_login_at timestamptz NULL,
            created_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE products (
            id bigserial PRIMARY KEY,
            code varchar(64) NOT NULL UNIQUE,
            public_token varchar(26) NOT NULL UNIQUE,
            status varchar(16) NOT NULL DEFAULT 'active',
            current_version_id bigint NULL,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            CONSTRAINT ck_products_code_format
                CHECK (code ~ '^[A-Z0-9_-]{1,64}$'),
            CONSTRAINT ck_products_status
                CHECK (status IN ('active', 'disabled'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE pdf_files (
            id bigserial PRIMARY KEY,
            sha256 char(64) NOT NULL UNIQUE,
            size_bytes bigint NOT NULL,
            storage_path text NOT NULL,
            created_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE pdf_versions (
            id bigserial PRIMARY KEY,
            product_id bigint NOT NULL REFERENCES products (id),
            pdf_file_id bigint NOT NULL REFERENCES pdf_files (id),
            version_no integer NOT NULL,
            original_filename text NOT NULL,
            uploaded_by bigint NOT NULL REFERENCES admins (id),
            uploaded_at timestamptz NOT NULL,
            CONSTRAINT uq_pdf_versions_product_version
                UNIQUE (product_id, version_no),
            CONSTRAINT uq_pdf_versions_product_id
                UNIQUE (product_id, id)
        )
        """
    )
    op.execute(
        """
        ALTER TABLE products
        ADD CONSTRAINT fk_products_current_version
        FOREIGN KEY (id, current_version_id)
        REFERENCES pdf_versions (product_id, id)
        """
    )
    op.execute(
        """
        CREATE TABLE admin_sessions (
            id uuid PRIMARY KEY,
            admin_id bigint NOT NULL REFERENCES admins (id),
            token_hash char(64) NOT NULL UNIQUE,
            issued_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE audit_events (
            id bigserial PRIMARY KEY,
            occurred_at timestamptz NOT NULL,
            actor_type varchar(16) NOT NULL,
            actor_id bigint NULL,
            action varchar(48) NOT NULL,
            target_type varchar(24) NULL,
            target_id bigint NULL,
            product_code varchar(64) NULL,
            result varchar(16) NOT NULL,
            request_id uuid NULL,
            detail jsonb NULL
        )
        """
    )

    op.execute(
        "CREATE INDEX ix_pdf_versions_product_history ON pdf_versions (product_id, version_no DESC)"
    )
    op.execute("CREATE INDEX ix_audit_events_occurred_at ON audit_events (occurred_at DESC)")
    op.execute(
        "CREATE INDEX ix_audit_events_product_occurred "
        "ON audit_events (product_code, occurred_at DESC)"
    )

    op.execute(
        """
        CREATE FUNCTION reject_immutable_product_columns()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.code IS DISTINCT FROM OLD.code THEN
                RAISE EXCEPTION 'product code is immutable';
            END IF;
            IF NEW.public_token IS DISTINCT FROM OLD.public_token THEN
                RAISE EXCEPTION 'public token is immutable';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id THEN
                RAISE EXCEPTION 'product id is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_products_immutable
        BEFORE UPDATE ON products
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_product_columns()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_append_only_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is forbidden', TG_TABLE_NAME, TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pdf_versions_append_only
        BEFORE UPDATE OR DELETE ON pdf_versions
        FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pdf_files_append_only
        BEFORE UPDATE OR DELETE ON pdf_files
        FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()
        """
    )

    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_rw, app_backup")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM app_rw, app_backup")
    op.execute("GRANT USAGE ON SCHEMA public TO app_rw, app_backup")
    op.execute(
        """
        GRANT SELECT, INSERT
        ON products, pdf_files, pdf_versions, admins, audit_events
        TO app_rw
        """
    )
    op.execute(
        """
        GRANT UPDATE (status, current_version_id, updated_at)
        ON products TO app_rw
        """
    )
    op.execute(
        """
        GRANT UPDATE (
            password_hash,
            must_change_password,
            password_updated_at,
            last_login_at
        ) ON admins TO app_rw
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON admin_sessions TO app_rw")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_rw")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_backup")
    op.execute("GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO app_backup")
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES FOR ROLE app_migrate IN SCHEMA public
        GRANT SELECT ON TABLES TO app_backup
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES FOR ROLE app_migrate IN SCHEMA public
        GRANT SELECT ON SEQUENCES TO app_backup
        """
    )


def downgrade() -> None:
    """Remove the complete initial schema while preserving database roles."""

    op.execute(
        """
        ALTER DEFAULT PRIVILEGES FOR ROLE app_migrate IN SCHEMA public
        REVOKE SELECT ON TABLES FROM app_backup
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES FOR ROLE app_migrate IN SCHEMA public
        REVOKE SELECT ON SEQUENCES FROM app_backup
        """
    )
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_rw, app_backup")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM app_rw, app_backup")
    op.execute("REVOKE USAGE ON SCHEMA public FROM app_rw, app_backup")
    op.execute("DROP TRIGGER trg_pdf_files_append_only ON pdf_files")
    op.execute("DROP TRIGGER trg_pdf_versions_append_only ON pdf_versions")
    op.execute("DROP TRIGGER trg_products_immutable ON products")
    op.execute("DROP FUNCTION reject_append_only_mutation()")
    op.execute("DROP FUNCTION reject_immutable_product_columns()")
    op.execute("ALTER TABLE products DROP CONSTRAINT fk_products_current_version")
    op.execute("DROP TABLE audit_events")
    op.execute("DROP TABLE admin_sessions")
    op.execute("DROP TABLE pdf_versions")
    op.execute("DROP TABLE pdf_files")
    op.execute("DROP TABLE products")
    op.execute("DROP TABLE admins")
