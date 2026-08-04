"""Uniform server-side authentication for every management route."""

from __future__ import annotations

from typing import cast
from urllib.parse import urlencode

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import RedirectResponse, Response

from product_pdf_qr.config import Settings, get_settings
from product_pdf_qr.database import Database
from product_pdf_qr.domains.auth import SESSION_COOKIE_NAME, resolve_session

LOGIN_PATH = "/admin/login"
CHANGE_PASSWORD_PATH = "/admin/change-password"
LOGOUT_PATH = "/admin/logout"


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

        request.state.admin = admin
        if admin.must_change_password and path not in {
            CHANGE_PASSWORD_PATH,
            LOGOUT_PATH,
        }:
            return self._redirect(CHANGE_PASSWORD_PATH)
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
