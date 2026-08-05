"""Server-rendered administrator login, password change, and management shell."""

from __future__ import annotations

import hmac
import math
from pathlib import Path
from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from product_pdf_qr.config import Settings
from product_pdf_qr.database import Database
from product_pdf_qr.dependencies import (
    get_database,
    get_login_rate_limiter,
    get_password_manager,
    get_runtime_settings,
)
from product_pdf_qr.domains.audit import AuditEvent, append_independent_event
from product_pdf_qr.domains.auth import (
    SESSION_COOKIE_NAME,
    AuthenticatedAdmin,
    LoginRateLimiter,
    PasswordManager,
    change_password,
    create_authenticated_session,
    revoke_session,
)
from product_pdf_qr.domains.importer import ImportResult, ImportRowError, import_products
from product_pdf_qr.errors import AppError

router = APIRouter(prefix="/admin", include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _safe_next_path(candidate: str) -> str:
    if candidate == "/admin" or candidate.startswith("/admin/"):
        return candidate
    return "/admin"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _login_response(
    request: Request,
    *,
    next_path: str,
    error: str | None = None,
    status_code: int = 200,
    retry_after: int | None = None,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": error,
            "next_path": _safe_next_path(next_path),
        },
        status_code=status_code,
    )
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    response.headers["Cache-Control"] = "no-store"
    return response


def _password_response(
    request: Request,
    admin: AuthenticatedAdmin,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name="change_password.html",
        context={
            "admin": admin,
            "csrf_token": cast(str, request.state.csrf_token),
            "error": error,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def _record_login_failure(
    database: Database,
    *,
    account_key: str,
    reason: str,
) -> None:
    await append_independent_event(
        database,
        AuditEvent(
            action="login_failure",
            result="failure",
            actor_type="anonymous",
            target_type="admin",
            detail={"username": account_key, "reason": reason},
        ),
    )


def _valid_csrf_token(request: Request, candidate: str | None) -> bool:
    expected = getattr(request.state, "csrf_token", None)
    return (
        isinstance(expected, str)
        and candidate is not None
        and hmac.compare_digest(candidate, expected)
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/admin") -> HTMLResponse:
    """Render the public login page without exposing management data."""

    return _login_response(request, next_path=next)


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_runtime_settings)],
    password_manager: Annotated[PasswordManager, Depends(get_password_manager)],
    limiter: Annotated[LoginRateLimiter, Depends(get_login_rate_limiter)],
    next_path: Annotated[str, Form(alias="next")] = "/admin",
) -> Response:
    """Verify credentials, apply dual backoff, and issue a hashed server session."""

    ip_address = _client_ip(request)
    account_key = username.strip()[:64].casefold()
    reservation = limiter.reserve_attempt(ip_address, account_key)
    if not reservation.allowed:
        wait_seconds = max(1, math.ceil(reservation.retry_after))
        await _record_login_failure(
            database,
            account_key=account_key,
            reason="rate_limited",
        )
        return _login_response(
            request,
            next_path=next_path,
            error=f"登录尝试过于频繁, 请在 {wait_seconds} 秒后重试。",
            status_code=429,
            retry_after=wait_seconds,
        )

    session = await create_authenticated_session(
        database,
        password_manager,
        raw_username=username,
        password=password,
        ttl_seconds=settings.session_ttl_seconds,
    )
    if session is None:
        await _record_login_failure(
            database,
            account_key=account_key,
            reason="invalid_credentials",
        )
        failure_wait_seconds: int | None = (
            math.ceil(reservation.retry_after) if reservation.retry_after > 0 else None
        )
        message = "用户名或密码错误。"
        if failure_wait_seconds is not None:
            message += f" 请在 {failure_wait_seconds} 秒后重试。"
        return _login_response(
            request,
            next_path=next_path,
            error=message,
            status_code=401,
            retry_after=failure_wait_seconds,
        )

    limiter.register_success(ip_address, account_key)
    destination = (
        "/admin/change-password"
        if session.admin.must_change_password
        else _safe_next_path(next_path)
    )
    response = RedirectResponse(
        url=destination,
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session.token,
        max_age=settings.session_ttl_seconds,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request) -> HTMLResponse:
    """Render the only management page allowed during forced password change."""

    admin = cast(AuthenticatedAdmin, request.state.admin)
    return _password_response(request, admin)


@router.post("/change-password", response_class=HTMLResponse)
async def change_password_endpoint(
    request: Request,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
    database: Annotated[Database, Depends(get_database)],
    password_manager: Annotated[PasswordManager, Depends(get_password_manager)],
    csrf_token: Annotated[str | None, Form()] = None,
) -> Response:
    """Change the password and revoke every other session."""

    admin = cast(AuthenticatedAdmin, request.state.admin)
    if not _valid_csrf_token(request, csrf_token):
        return _password_response(
            request,
            admin,
            error="请求验证失败, 请刷新页面后重试。",
            status_code=403,
        )
    if new_password != confirm_password:
        return _password_response(
            request,
            admin,
            error="两次输入的新密码不一致。",
            status_code=422,
        )
    try:
        await change_password(
            database,
            password_manager,
            admin,
            current_password=current_password,
            new_password=new_password,
        )
    except AppError as error:
        return _password_response(
            request,
            admin,
            error=error.message,
            status_code=error.status_code,
        )
    return RedirectResponse(
        url="/admin",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/logout")
async def logout(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_runtime_settings)],
    csrf_token: Annotated[str | None, Form()] = None,
) -> Response:
    """Revoke the current server-side session and remove its browser cookie."""

    if not _valid_csrf_token(request, csrf_token):
        return HTMLResponse(
            "请求验证失败, 请刷新页面后重试。",
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )
    admin = cast(AuthenticatedAdmin, request.state.admin)
    await revoke_session(database, admin)
    response = RedirectResponse(
        url="/admin/login",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("", response_class=HTMLResponse)
@router.get("/products/{product_id}", response_class=HTMLResponse)
async def admin_page(request: Request, product_id: int | None = None) -> HTMLResponse:
    """Render the authenticated client-side management shell."""

    admin = cast(AuthenticatedAdmin, request.state.admin)
    response = templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "product_id": product_id,
            "admin": admin,
            "csrf_token": cast(str, request.state.csrf_token),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/imports", response_class=HTMLResponse)
async def import_page(request: Request) -> HTMLResponse:
    """Render the authenticated XLSX upload entry and result surface."""

    admin = cast(AuthenticatedAdmin, request.state.admin)
    response = templates.TemplateResponse(
        request=request,
        name="import.html",
        context={
            "admin": admin,
            "csrf_token": cast(str, request.state.csrf_token),
            "result": None,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/imports", response_class=HTMLResponse)
async def submit_import_page(
    request: Request,
    file: Annotated[UploadFile, File()],
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_runtime_settings)],
    csrf_token: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Validate CSRF, execute one import, and server-render every result entry."""

    admin = cast(AuthenticatedAdmin, request.state.admin)
    if not _valid_csrf_token(request, csrf_token):
        await file.close()
        await append_independent_event(
            database,
            AuditEvent(
                action="product_import",
                result="failure",
                actor_type="admin",
                actor_id=admin.id,
                target_type="product_batch",
                detail={"reason": "csrf_rejection", "actual": "missing_or_mismatch"},
            ),
        )
        result = ImportResult(
            success_count=0,
            duplicate_count=0,
            format_error_count=0,
            errors=(
                ImportRowError(
                    row=None,
                    reason="请求验证失败, 请刷新页面后重试。",
                    kind="authentication",
                ),
            ),
            status="failure",
            error_code="invalid_csrf_token",
            http_status=403,
        )
    else:
        result = await import_products(
            database,
            file,
            settings,
            actor_id=admin.id,
            request_id=uuid4(),
        )
    response = templates.TemplateResponse(
        request=request,
        name="import.html",
        context={
            "admin": admin,
            "csrf_token": cast(str, request.state.csrf_token),
            "result": result,
        },
        status_code=result.http_status,
    )
    response.headers["Cache-Control"] = "no-store"
    return response
