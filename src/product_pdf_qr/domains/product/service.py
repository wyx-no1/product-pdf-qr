"""Product creation, code normalization, and public-token generation."""

from __future__ import annotations

import base64
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from psycopg.errors import UniqueViolation

from product_pdf_qr.database import Database
from product_pdf_qr.domains.audit import AuditEvent, append_event
from product_pdf_qr.errors import AppError

PRODUCT_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$", re.ASCII)
NORMALIZED_PRODUCT_CODE_PATTERN = re.compile(r"^[A-Z0-9_-]{1,64}$", re.ASCII)
PUBLIC_TOKEN_BYTES = 16
PUBLIC_TOKEN_LENGTH = 26
TOKEN_RETRY_LIMIT = 5
PRODUCT_NAME_MAX_LENGTH = 120
ProductPDFStatus = Literal["uploaded", "not_uploaded"]


@dataclass(frozen=True, slots=True)
class Product:
    """A product projection returned by the product domain."""

    id: int
    code: str
    name: str | None
    public_token: str
    status: str
    current_version_id: int | None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def normalize_product_code(raw_code: str) -> str:
    """Validate one product-code input and return its canonical uppercase form."""

    stripped = raw_code.strip()
    if PRODUCT_CODE_PATTERN.fullmatch(stripped) is None:
        raise AppError(
            "invalid_product_code",
            "产品编码须为 1-64 位英文字母、数字、横线或下划线。",
            422,
        )
    return stripped.upper()


def is_normalized_product_code(code: str) -> bool:
    """Return whether a stored code satisfies the canonical contract."""

    return NORMALIZED_PRODUCT_CODE_PATTERN.fullmatch(code) is not None


def normalize_product_name(raw_name: str) -> str:
    """Return a required, trimmed product name within the V1 storage limit."""

    name = raw_name.strip()
    if not name or len(name) > PRODUCT_NAME_MAX_LENGTH:
        raise AppError(
            "invalid_product_name",
            "产品名称须为 1-120 个字符。",
            422,
        )
    return name


def generate_public_token() -> str:
    """Generate an unpadded Base32 token with exactly 128 bits of CSPRNG entropy."""

    token = base64.b32encode(secrets.token_bytes(PUBLIC_TOKEN_BYTES)).decode("ascii").rstrip("=")
    if len(token) != PUBLIC_TOKEN_LENGTH:
        raise RuntimeError("Unexpected public token length")
    return token


def _product_from_row(row: dict[str, object]) -> Product:
    raw_name = row.get("name")
    return Product(
        id=cast(int, row["id"]),
        code=str(row["code"]),
        name=str(raw_name) if raw_name is not None else None,
        public_token=str(row["public_token"]),
        status=str(row["status"]),
        current_version_id=(
            cast(int, row["current_version_id"]) if row["current_version_id"] is not None else None
        ),
        created_at=cast(datetime | None, row.get("created_at")),
        updated_at=cast(datetime | None, row.get("updated_at")),
    )


async def create_product(
    database: Database,
    raw_code: str,
    raw_name: str,
    *,
    actor_id: int,
    request_id: UUID | None = None,
) -> Product:
    """Atomically create a product and its success audit event."""

    code = normalize_product_code(raw_code)
    name = normalize_product_name(raw_name)
    for _attempt in range(TOKEN_RETRY_LIMIT):
        token = generate_public_token()
        try:
            async with database.connection() as connection:
                async with connection.transaction():
                    cursor = await connection.execute(
                        """
                        INSERT INTO products (
                            code,
                            name,
                            public_token,
                            created_at,
                            updated_at
                        ) VALUES (%s, %s, %s, now(), now())
                        RETURNING
                            id,
                            code,
                            name,
                            public_token,
                            status,
                            current_version_id,
                            created_at,
                            updated_at
                        """,
                        (code, name, token),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        raise RuntimeError("Product insert returned no row")
                    product = _product_from_row(row)
                    await append_event(
                        connection,
                        AuditEvent(
                            action="product_create",
                            result="success",
                            actor_type="admin",
                            actor_id=actor_id,
                            target_type="product",
                            target_id=product.id,
                            product_code=product.code,
                            request_id=request_id,
                        ),
                    )
            return product
        except UniqueViolation as error:
            constraint_name = error.diag.constraint_name
            if constraint_name == "products_code_key":
                raise AppError("duplicate_product_code", "产品编码已存在。", 409) from error
            if constraint_name == "products_public_token_key":
                continue
            raise
    raise AppError("token_generation_failed", "无法生成公开标识, 请重试。", 503)


def _escape_like_pattern(value: str) -> str:
    """Escape PostgreSQL LIKE metacharacters before adding substring wildcards."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_products(
    database: Database,
    *,
    limit: int,
    offset: int,
    q: str | None = None,
    pdf_status: ProductPDFStatus | None = None,
) -> list[Product]:
    """Filter in PostgreSQL, then return one stable page of matching products."""

    clauses: list[str] = []
    parameters: list[object] = []
    search_term = q.strip() if q is not None else ""
    if search_term:
        pattern = f"%{_escape_like_pattern(search_term)}%"
        clauses.append(
            """
            (
                code ILIKE %s ESCAPE E'\\\\'
                OR COALESCE(name, '') ILIKE %s ESCAPE E'\\\\'
            )
            """
        )
        parameters.extend((pattern, pattern))
    if pdf_status == "uploaded":
        clauses.append("current_version_id IS NOT NULL")
    elif pdf_status == "not_uploaded":
        clauses.append("current_version_id IS NULL")

    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    parameters.extend((limit, offset))
    async with database.connection() as connection:
        cursor = await connection.execute(
            f"""
            SELECT
                id,
                code,
                name,
                public_token,
                status,
                current_version_id,
                created_at,
                updated_at
            FROM products
            {where_clause}
            ORDER BY updated_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            tuple(parameters),
        )
        rows = await cursor.fetchall()
    return [_product_from_row(row) for row in rows]


async def get_product(database: Database, product_id: int) -> Product:
    """Load one product or raise the shared safe not-found response."""

    async with database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT
                id,
                code,
                name,
                public_token,
                status,
                current_version_id,
                created_at,
                updated_at
            FROM products
            WHERE id = %s
            """,
            (product_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise AppError("product_not_found", "产品不存在。", 404)
    return _product_from_row(row)
