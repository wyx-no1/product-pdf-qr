"""Administrator identity domain."""

from product_pdf_qr.domains.auth.rate_limit import LoginRateLimiter
from product_pdf_qr.domains.auth.service import (
    ADMIN_USERNAME_MAX_LENGTH,
    CSRF_HEADER_NAME,
    PASSWORD_MIN_LENGTH,
    SESSION_COOKIE_NAME,
    AuthenticatedAdmin,
    CreatedSession,
    PasswordManager,
    change_password,
    create_admin,
    create_authenticated_session,
    csrf_token_for_session,
    hash_session_token,
    normalize_username,
    reset_admin_password,
    resolve_session,
    revoke_session,
    validate_new_password,
)

__all__ = [
    "ADMIN_USERNAME_MAX_LENGTH",
    "CSRF_HEADER_NAME",
    "PASSWORD_MIN_LENGTH",
    "SESSION_COOKIE_NAME",
    "AuthenticatedAdmin",
    "CreatedSession",
    "LoginRateLimiter",
    "PasswordManager",
    "change_password",
    "create_admin",
    "create_authenticated_session",
    "csrf_token_for_session",
    "hash_session_token",
    "normalize_username",
    "reset_admin_password",
    "resolve_session",
    "revoke_session",
    "validate_new_password",
]
