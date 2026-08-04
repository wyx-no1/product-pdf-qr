"""Argon2id administrator credentials and server-side sessions."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from product_pdf_qr.database import Database
from product_pdf_qr.domains.audit import AuditEvent, append_event
from product_pdf_qr.errors import AppError

ADMIN_USERNAME_MAX_LENGTH = 64
PASSWORD_MIN_LENGTH = 12
SESSION_TOKEN_BYTES = 32
SESSION_COOKIE_NAME = "admin_session"
ADMIN_CREATE_ADVISORY_LOCK_ID = 7_328_011_001


@dataclass(frozen=True, slots=True)
class AuthenticatedAdmin:
    """The non-secret administrator identity attached to an authenticated request."""

    id: int
    username: str
    must_change_password: bool
    session_id: UUID
    session_expires_at: datetime


@dataclass(frozen=True, slots=True)
class CreatedSession:
    """A one-time raw browser token and its safe request identity."""

    token: str
    admin: AuthenticatedAdmin


class PasswordManager:
    """Hash and verify passwords with the reviewed Argon2id profile."""

    def __init__(self) -> None:
        self._hasher = PasswordHasher(
            time_cost=3,
            memory_cost=64 * 1024,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(SESSION_TOKEN_BYTES))

    def hash(self, password: str) -> str:
        """Return an Argon2id hash; callers must never log either input or output."""

        return self._hasher.hash(password)

    def verify(self, password_hash: str, candidate: str) -> bool:
        """Return false for a mismatch or malformed persisted hash."""

        try:
            return self._hasher.verify(password_hash, candidate)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def consume_dummy_verification(self, candidate: str) -> None:
        """Spend one normal verification for an unknown username."""

        self.verify(self._dummy_hash, candidate)


def normalize_username(raw_username: str) -> str:
    """Return the bounded username accepted by the existing schema."""

    username = raw_username.strip()
    if not username or len(username) > ADMIN_USERNAME_MAX_LENGTH:
        raise AppError(
            "invalid_admin_username",
            "管理员用户名须为 1-64 个字符。",
            422,
        )
    return username


def validate_new_password(password: str, username: str) -> None:
    """Enforce the V1 password strength rules without character-class mandates."""

    if len(password) < PASSWORD_MIN_LENGTH:
        raise AppError(
            "weak_password",
            f"密码至少需要 {PASSWORD_MIN_LENGTH} 个字符。",
            422,
        )
    if password.casefold() == username.casefold():
        raise AppError(
            "weak_password",
            "密码不得与管理员用户名相同。",
            422,
        )


def hash_session_token(token: str) -> str:
    """Return the only representation of a session token persisted in PostgreSQL."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_admin(
    database: Database,
    password_manager: PasswordManager,
    *,
    raw_username: str,
    password: str,
) -> int:
    """Create the sole administrator with a forced first-password change."""

    username = normalize_username(raw_username)
    validate_new_password(password, username)
    password_hash = await asyncio.to_thread(password_manager.hash, password)
    async with database.connection() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (ADMIN_CREATE_ADVISORY_LOCK_ID,),
            )
            existing_cursor = await connection.execute(
                "SELECT id, username FROM admins ORDER BY id LIMIT 1"
            )
            existing = await existing_cursor.fetchone()
            if existing is not None:
                code = (
                    "admin_already_exists"
                    if str(existing["username"]).casefold() == username.casefold()
                    else "single_admin_already_exists"
                )
                raise AppError(code, "管理员已存在, 未覆盖现有账号。", 409)
            cursor = await connection.execute(
                """
                INSERT INTO admins (
                    username,
                    password_hash,
                    must_change_password,
                    password_updated_at,
                    created_at
                ) VALUES (%s, %s, true, now(), now())
                RETURNING id
                """,
                (username, password_hash),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("Administrator insert returned no row")
            admin_id = cast(int, row["id"])
            await append_event(
                connection,
                AuditEvent(
                    action="admin_create",
                    result="success",
                    actor_type="system",
                    target_type="admin",
                    target_id=admin_id,
                ),
            )
    return admin_id


async def reset_admin_password(
    database: Database,
    password_manager: PasswordManager,
    *,
    raw_username: str,
    password: str,
) -> int:
    """Set a temporary password, force change, and revoke every active session."""

    username = normalize_username(raw_username)
    validate_new_password(password, username)
    password_hash = await asyncio.to_thread(password_manager.hash, password)
    async with database.connection() as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                SELECT id
                FROM admins
                WHERE username = %s
                FOR UPDATE
                """,
                (username,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise AppError("admin_not_found", "管理员不存在。", 404)
            admin_id = cast(int, row["id"])
            await connection.execute(
                """
                UPDATE admins
                SET
                    password_hash = %s,
                    must_change_password = true,
                    password_updated_at = now()
                WHERE id = %s
                """,
                (password_hash, admin_id),
            )
            await connection.execute(
                """
                UPDATE admin_sessions
                SET revoked_at = now()
                WHERE admin_id = %s AND revoked_at IS NULL
                """,
                (admin_id,),
            )
            await append_event(
                connection,
                AuditEvent(
                    action="password_reset",
                    result="success",
                    actor_type="system",
                    target_type="admin",
                    target_id=admin_id,
                ),
            )
    return admin_id


async def create_authenticated_session(
    database: Database,
    password_manager: PasswordManager,
    *,
    raw_username: str,
    password: str,
    ttl_seconds: int,
) -> CreatedSession | None:
    """Verify credentials and atomically create a server-side session."""

    try:
        username = normalize_username(raw_username)
    except AppError:
        await asyncio.to_thread(password_manager.consume_dummy_verification, password)
        return None

    async with database.connection() as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                SELECT id, username, password_hash, must_change_password
                FROM admins
                WHERE username = %s
                FOR UPDATE
                """,
                (username,),
            )
            row = await cursor.fetchone()
            if row is None:
                await asyncio.to_thread(password_manager.consume_dummy_verification, password)
                return None
            password_hash = str(row["password_hash"])
            verified = await asyncio.to_thread(
                password_manager.verify,
                password_hash,
                password,
            )
            if not verified:
                return None

            admin_id = cast(int, row["id"])
            session_id = uuid4()
            token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
            token_hash = hash_session_token(token)
            session_cursor = await connection.execute(
                """
                INSERT INTO admin_sessions (
                    id,
                    admin_id,
                    token_hash,
                    issued_at,
                    expires_at
                ) VALUES (%s, %s, %s, now(), now() + (%s * interval '1 second'))
                RETURNING expires_at
                """,
                (session_id, admin_id, token_hash, ttl_seconds),
            )
            session_row = await session_cursor.fetchone()
            if session_row is None:
                raise RuntimeError("Session insert returned no row")
            await connection.execute(
                "UPDATE admins SET last_login_at = now() WHERE id = %s",
                (admin_id,),
            )
            await append_event(
                connection,
                AuditEvent(
                    action="admin_login",
                    result="success",
                    actor_type="admin",
                    actor_id=admin_id,
                    target_type="admin",
                    target_id=admin_id,
                ),
            )
    return CreatedSession(
        token=token,
        admin=AuthenticatedAdmin(
            id=admin_id,
            username=str(row["username"]),
            must_change_password=bool(row["must_change_password"]),
            session_id=session_id,
            session_expires_at=cast(datetime, session_row["expires_at"]),
        ),
    )


async def resolve_session(database: Database, token: str) -> AuthenticatedAdmin | None:
    """Resolve a non-expired, non-revoked hashed session token."""

    token_hash = hash_session_token(token)
    async with database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT
                s.id AS session_id,
                s.expires_at,
                a.id AS admin_id,
                a.username,
                a.must_change_password
            FROM admin_sessions AS s
            JOIN admins AS a ON a.id = s.admin_id
            WHERE s.token_hash = %s
              AND s.revoked_at IS NULL
              AND s.expires_at > now()
            """,
            (token_hash,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return AuthenticatedAdmin(
        id=cast(int, row["admin_id"]),
        username=str(row["username"]),
        must_change_password=bool(row["must_change_password"]),
        session_id=cast(UUID, row["session_id"]),
        session_expires_at=cast(datetime, row["expires_at"]),
    )


async def revoke_session(database: Database, admin: AuthenticatedAdmin) -> None:
    """Revoke the current session and append a secret-free audit event."""

    async with database.connection() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                UPDATE admin_sessions
                SET revoked_at = now()
                WHERE id = %s AND admin_id = %s AND revoked_at IS NULL
                """,
                (admin.session_id, admin.id),
            )
            await append_event(
                connection,
                AuditEvent(
                    action="admin_logout",
                    result="success",
                    actor_type="admin",
                    actor_id=admin.id,
                    target_type="admin",
                    target_id=admin.id,
                ),
            )


async def change_password(
    database: Database,
    password_manager: PasswordManager,
    admin: AuthenticatedAdmin,
    *,
    current_password: str,
    new_password: str,
) -> None:
    """Change the password, retain only this session, and clear the force flag."""

    validate_new_password(new_password, admin.username)
    async with database.connection() as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                SELECT password_hash
                FROM admins
                WHERE id = %s
                FOR UPDATE
                """,
                (admin.id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise AppError("admin_not_found", "管理员不存在。", 404)
            current_hash = str(row["password_hash"])
            current_matches = await asyncio.to_thread(
                password_manager.verify,
                current_hash,
                current_password,
            )
            if not current_matches:
                raise AppError("invalid_current_password", "当前密码不正确。", 422)
            unchanged = await asyncio.to_thread(
                password_manager.verify,
                current_hash,
                new_password,
            )
            if unchanged:
                raise AppError(
                    "password_unchanged",
                    "新密码必须与当前密码不同。",
                    422,
                )
            new_hash = await asyncio.to_thread(password_manager.hash, new_password)
            await connection.execute(
                """
                UPDATE admins
                SET
                    password_hash = %s,
                    must_change_password = false,
                    password_updated_at = now()
                WHERE id = %s
                """,
                (new_hash, admin.id),
            )
            await connection.execute(
                """
                UPDATE admin_sessions
                SET revoked_at = now()
                WHERE admin_id = %s
                  AND id <> %s
                  AND revoked_at IS NULL
                """,
                (admin.id, admin.session_id),
            )
            await append_event(
                connection,
                AuditEvent(
                    action="password_change",
                    result="success",
                    actor_type="admin",
                    actor_id=admin.id,
                    target_type="admin",
                    target_id=admin.id,
                ),
            )
