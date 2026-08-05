"""Uniform server-side authentication for every management route."""

from __future__ import annotations

import hmac
from typing import cast
from urllib.parse import urlencode

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, RedirectResponse, Response

from product_pdf_qr.config import Settings, get_settings
from product_pdf_qr.database import Database
from product_pdf_qr.domains.auth import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    csrf_token_for_session,
    resolve_session,
)

LOGIN_PATH = "/admin/login"
CHANGE_PASSWORD_PATH = "/admin/change-password"
LOGOUT_PATH = "/admin/logout"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class AdminAuthenticationMiddleware(BaseHTTPMiddleware):
    """Authenticate `/admin` and `/api` before any business handler runs."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        path = request.url.path
        if not self._is_management_path(path) or path == LOGIN_PATH:
            return await call_next(request)

        settings = self._settings(request)
        token = request.cookies.get(SESSION_COOKIE_NAME)
        database = getattr(request.app.state, "database", None)
        admin = None
        if token is not None and database is not None:
            admin = await resolve_session(cast(Database, database), token)
        if admin is None:
            return self._redirect_without_session(request, settings)

        assert token is not None
        request.state.admin = admin
        csrf_token = csrf_token_for_session(token)
        request.state.csrf_token = csrf_token
        if admin.must_change_password and path not in {
            CHANGE_PASSWORD_PATH,
            LOGOUT_PATH,
        }:
            return self._redirect(CHANGE_PASSWORD_PATH)
        if (
            path.startswith("/api/")
            and request.method not in SAFE_METHODS
            and not hmac.compare_digest(
                request.headers.get(CSRF_HEADER_NAME, ""),
                csrf_token,
            )
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "invalid_csrf_token",
                        "message": "请求验证失败, 请刷新管理页面后重试。",
                    }
                },
                headers={"Cache-Control": "no-store"},
            )
        return await call_next(request)

    @staticmethod
    def _is_management_path(path: str) -> bool:
        return path == "/admin" or path.startswith("/admin/") or path.startswith("/api/")

    @staticmethod
    def _settings(request: Request) -> Settings:
        settings = getattr(request.app.state, "settings", None)
        return settings if isinstance(settings, Settings) else get_settings()

    def _redirect_without_session(
        self,
        request: Request,
        settings: Settings,
    ) -> RedirectResponse:
        query = urlencode({"next": request.url.path})
        response = self._redirect(f"{LOGIN_PATH}?{query}")
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path="/",
            secure=settings.session_cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response

    @staticmethod
    def _redirect(url: str) -> RedirectResponse:
        return RedirectResponse(
            url=url,
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )
