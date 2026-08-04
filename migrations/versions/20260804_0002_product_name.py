"""Add the nullable V1 product name.

Revision ID: 20260804_0002
Revises: 20260801_0001
Create Date: 2026-08-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260804_0002"
down_revision: str | None = "20260801_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a bounded nullable name without invalidating existing products."""

    op.execute("ALTER TABLE products ADD COLUMN name varchar(120) NULL")


def downgrade() -> None:
    """Remove only the V1 product-name column."""

    op.execute("ALTER TABLE products DROP COLUMN name")
