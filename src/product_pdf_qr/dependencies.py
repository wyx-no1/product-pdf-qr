"""Shared FastAPI dependencies."""

from typing import cast

from fastapi import Request

from product_pdf_qr.config import Settings
from product_pdf_qr.database import Database
from product_pdf_qr.domains.auth import AuthenticatedAdmin, LoginRateLimiter, PasswordManager
from product_pdf_qr.domains.public import PublicMissLimiter
from product_pdf_qr.domains.qrcode import QRCodeService
from product_pdf_qr.domains.storage import StorageService


def get_database(request: Request) -> Database:
    """Return the application-owned runtime database."""

    return cast(Database, request.app.state.database)


def get_runtime_settings(request: Request) -> Settings:
    """Return the application-owned settings."""

    return cast(Settings, request.app.state.settings)


def get_storage_service(request: Request) -> StorageService:
    """Return the application-owned content storage."""

    return cast(StorageService, request.app.state.storage_service)


def get_qrcode_service(request: Request) -> QRCodeService:
    """Return the application-owned QR generator."""

    return cast(QRCodeService, request.app.state.qrcode_service)


def get_public_miss_limiter(request: Request) -> PublicMissLimiter:
    """Return the process-local missing-token limiter."""

    return cast(PublicMissLimiter, request.app.state.public_miss_limiter)


def get_password_manager(request: Request) -> PasswordManager:
    """Return the application-owned Argon2id password manager."""

    return cast(PasswordManager, request.app.state.password_manager)


def get_login_rate_limiter(request: Request) -> LoginRateLimiter:
    """Return the process-local dual-dimension login limiter."""

    return cast(LoginRateLimiter, request.app.state.login_rate_limiter)


def get_current_admin(request: Request) -> AuthenticatedAdmin:
    """Return the identity populated by the authentication middleware."""

    return cast(AuthenticatedAdmin, request.state.admin)
