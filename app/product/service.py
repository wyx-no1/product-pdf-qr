import base64
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from psycopg.errors import UniqueViolation

from app.audit.service import write_audit
from app.db import Pool

PRODUCT_CODE_PATTERN = re.compile(r"^[A-Z0-9_-]{1,64}$", re.ASCII)
PUBLIC_TOKEN_PATTERN = re.compile(r"^[A-Z2-7]{26}$", re.ASCII)


class InvalidProductCode(ValueError):
    pass


class DuplicateProductCode(ValueError):
    pass


@dataclass(frozen=True)
class Product:
    id: int
    code: str
    public_token: str
    status: str
    current_version_id: int | None


def normalize_product_code(raw_code: str) -> str:
    stripped = raw_code.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", stripped, re.ASCII):
        raise InvalidProductCode(
            "产品编码须为 1–64 个英文字母、数字、横线或下划线"
        )
    return stripped.upper()


def generate_public_token() -> str:
    token = base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")
    if not PUBLIC_TOKEN_PATTERN.fullmatch(token):
        raise RuntimeError("generated token violated the public token contract")
    return token


def product_from_row(row: dict[str, Any]) -> Product:
    return Product(
        id=int(row["id"]),
        code=str(row["code"]),
        public_token=str(row["public_token"]),
        status=str(row["status"]),
        current_version_id=(
            int(row["current_version_id"]) if row["current_version_id"] is not None else None
        ),
    )


async def create_product(pool: Pool, raw_code: str) -> Product:
    code = normalize_product_code(raw_code)
    request_id = uuid.uuid4()
    async with pool.connection() as conn:
        for _ in range(5):
            token = generate_public_token()
            try:
                async with conn.transaction():
                    cursor = await conn.execute(
                        """
                        INSERT INTO products (code, public_token)
                        VALUES (%s, %s)
                        RETURNING id, code, public_token, status, current_version_id
                        """,
                        (code, token),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        raise RuntimeError("product INSERT returned no row")
                    await write_audit(
                        conn,
                        action="product_create",
                        result="success",
                        target_type="product",
                        target_id=int(row["id"]),
                        product_code=code,
                        request_id=request_id,
                    )
                return product_from_row(row)
            except UniqueViolation as exc:
                await conn.rollback()
                cursor = await conn.execute("SELECT 1 FROM products WHERE code = %s", (code,))
                if await cursor.fetchone() is not None:
                    raise DuplicateProductCode("产品编码已存在") from exc
        raise RuntimeError("public token collision retry limit exceeded")


async def get_product(pool: Pool, product_id: int) -> Product | None:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, code, public_token, status, current_version_id
            FROM products
            WHERE id = %s
            """,
            (product_id,),
        )
        row = await cursor.fetchone()
        return product_from_row(row) if row else None
