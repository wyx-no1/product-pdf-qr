"""Administrator password, session, and dual-dimension limiter tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from product_pdf_qr.domains.auth import (
    AuthenticatedAdmin,
    LoginRateLimiter,
    PasswordManager,
    change_password,
    create_admin,
    create_authenticated_session,
    hash_session_token,
    reset_admin_password,
    resolve_session,
    validate_new_password,
)
from product_pdf_qr.errors import AppError
from tests.unit.test_business_services import (
    ScriptedConnection,
    ScriptedDatabase,
    as_database,
)


class FastPasswordManager:
    """Deterministic stand-in for database orchestration tests."""

    def hash(self, _password: str) -> str:
        return "$argon2id$synthetic-hash"

    def verify(self, password_hash: str, candidate: str) -> bool:
        return password_hash == "$argon2id$current" and candidate == "CurrentPassword-123"

    def consume_dummy_verification(self, _candidate: str) -> None:
        return None


def fast_password_manager() -> PasswordManager:
    return cast(PasswordManager, FastPasswordManager())


def test_password_manager_uses_argon2id_and_strength_rules() -> None:
    manager = PasswordManager()
    password_hash = manager.hash("LongSyntheticPassword-123")

    assert password_hash.startswith("$argon2id$")
    assert manager.verify(password_hash, "LongSyntheticPassword-123")
    assert not manager.verify(password_hash, "wrong-password")
    assert not manager.verify("malformed", "LongSyntheticPassword-123")

    for password, username in (
        ("short", "admin"),
        ("SameAsUsername", "sameasusername"),
    ):
        with pytest.raises(AppError) as captured:
            validate_new_password(password, username)
        assert captured.value.code == "weak_password"


def test_login_limiter_blocks_by_ip_and_account_with_exponential_backoff() -> None:
    now = [100.0]
    limiter = LoginRateLimiter(
        failure_limit=2,
        window_seconds=60,
        base_backoff_seconds=1,
        max_backoff_seconds=8,
        clock=lambda: now[0],
    )

    assert limiter.register_failure("127.0.0.1", "Admin") == 0
    assert limiter.register_failure("127.0.0.1", "Admin") == 1
    assert limiter.retry_after("127.0.0.2", "admin") == 1
    assert limiter.retry_after("127.0.0.1", "different") == 1

    now[0] += 1
    assert limiter.register_failure("127.0.0.1", "ADMIN") == 2
    limiter.register_success("127.0.0.1", "admin")
    assert limiter.retry_after("127.0.0.1", "admin") == 0

    limiter.register_failure("127.0.0.1", "admin")
    now[0] += 61
    assert limiter.retry_after("127.0.0.1", "admin") == 0


@pytest.mark.anyio
async def test_cli_services_create_and_reset_single_admin_without_plaintext() -> None:
    create_connection = ScriptedConnection([None, None, {"id": 7}, None])
    password = "TemporaryPassword-123"

    admin_id = await create_admin(
        as_database(ScriptedDatabase(create_connection)),
        fast_password_manager(),
        raw_username=" owner ",
        password=password,
    )

    assert admin_id == 7
    assert "pg_advisory_xact_lock" in create_connection.queries[0]
    assert create_connection.parameters[2] == ("owner", "$argon2id$synthetic-hash")
    assert password not in repr(create_connection.parameters)

    reset_connection = ScriptedConnection(
        [
            {"id": 7},
            None,
            None,
            None,
        ]
    )
    reset_id = await reset_admin_password(
        as_database(ScriptedDatabase(reset_connection)),
        fast_password_manager(),
        raw_username="owner",
        password=password,
    )

    assert reset_id == 7
    assert "must_change_password = true" in reset_connection.queries[1]
    assert "UPDATE admin_sessions" in reset_connection.queries[2]
    assert password not in repr(reset_connection.parameters)


@pytest.mark.anyio
async def test_create_admin_refuses_to_overwrite_existing_account() -> None:
    connection = ScriptedConnection([None, {"id": 7, "username": "owner"}])

    with pytest.raises(AppError) as captured:
        await create_admin(
            as_database(ScriptedDatabase(connection)),
            fast_password_manager(),
            raw_username="owner",
            password="TemporaryPassword-123",
        )

    assert captured.value.code == "admin_already_exists"


@pytest.mark.anyio
async def test_login_creates_only_a_hashed_session_token() -> None:
    expires_at = datetime(2026, 8, 5, tzinfo=UTC)
    connection = ScriptedConnection(
        [
            {
                "id": 7,
                "username": "owner",
                "password_hash": "$argon2id$current",
                "must_change_password": True,
            },
            {"expires_at": expires_at},
            None,
            None,
        ]
    )

    session = await create_authenticated_session(
        as_database(ScriptedDatabase(connection)),
        fast_password_manager(),
        raw_username="owner",
        password="CurrentPassword-123",
        ttl_seconds=3600,
    )

    assert session is not None
    assert session.admin.must_change_password
    insert_parameters = cast(tuple[object, ...], connection.parameters[1])
    assert insert_parameters[2] == hash_session_token(session.token)
    assert session.token not in repr(connection.parameters)
    assert len(str(insert_parameters[2])) == 64
    assert "UPDATE admins SET last_login_at" in connection.queries[2]


@pytest.mark.anyio
async def test_unknown_or_wrong_login_never_creates_a_session() -> None:
    unknown = ScriptedConnection([None])
    assert (
        await create_authenticated_session(
            as_database(ScriptedDatabase(unknown)),
            fast_password_manager(),
            raw_username="missing",
            password="CurrentPassword-123",
            ttl_seconds=3600,
        )
        is None
    )

    wrong = ScriptedConnection(
        [
            {
                "id": 7,
                "username": "owner",
                "password_hash": "$argon2id$current",
                "must_change_password": False,
            }
        ]
    )
    assert (
        await create_authenticated_session(
            as_database(ScriptedDatabase(wrong)),
            fast_password_manager(),
            raw_username="owner",
            password="WrongPassword-123",
            ttl_seconds=3600,
        )
        is None
    )


@pytest.mark.anyio
async def test_resolve_and_change_password_revoke_other_sessions() -> None:
    expires_at = datetime(2026, 8, 5, tzinfo=UTC)
    session_id = UUID("11111111-1111-1111-1111-111111111111")
    resolved = await resolve_session(
        as_database(
            ScriptedDatabase(
                ScriptedConnection(
                    [
                        {
                            "session_id": session_id,
                            "expires_at": expires_at,
                            "admin_id": 7,
                            "username": "owner",
                            "must_change_password": True,
                        }
                    ]
                )
            )
        ),
        "raw-token",
    )
    assert resolved is not None
    assert resolved.id == 7

    change_connection = ScriptedConnection(
        [
            {"password_hash": "$argon2id$current"},
            None,
            None,
            None,
        ]
    )
    await change_password(
        as_database(ScriptedDatabase(change_connection)),
        fast_password_manager(),
        resolved,
        current_password="CurrentPassword-123",
        new_password="DifferentPassword-456",
    )

    assert "must_change_password = false" in change_connection.queries[1]
    assert "id <> %s" in change_connection.queries[2]
    assert change_connection.parameters[2] == (7, session_id)
    assert "password_change" in repr(change_connection.parameters[3])


@pytest.mark.anyio
async def test_change_password_rejects_current_password_reuse() -> None:
    admin = AuthenticatedAdmin(
        id=7,
        username="owner",
        must_change_password=True,
        session_id=UUID("11111111-1111-1111-1111-111111111111"),
        session_expires_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    connection = ScriptedConnection([{"password_hash": "$argon2id$current"}])

    with pytest.raises(AppError) as captured:
        await change_password(
            as_database(ScriptedDatabase(connection)),
            fast_password_manager(),
            admin,
            current_password="CurrentPassword-123",
            new_password="CurrentPassword-123",
        )

    assert captured.value.code == "password_unchanged"
