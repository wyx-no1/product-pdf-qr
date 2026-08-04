"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from product_pdf_qr.admin import router as admin_router
from product_pdf_qr.config import get_settings
from product_pdf_qr.database import Database
from product_pdf_qr.domains.product.router import router as product_router
from product_pdf_qr.domains.public import PublicMissLimiter
from product_pdf_qr.domains.public.router import router as public_router
from product_pdf_qr.domains.qrcode import QRCodeService
from product_pdf_qr.domains.qrcode.router import router as qrcode_router
from product_pdf_qr.domains.storage import StorageService
from product_pdf_qr.domains.storage.router import router as storage_router
from product_pdf_qr.errors import register_exception_handlers
from product_pdf_qr.upload_limit import UploadRequestLimitMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop infrastructure owned by the application process."""

    settings = get_settings()
    database = Database(settings)
    storage_service = StorageService(
        settings.storage_root,
        settings.max_pdf_bytes,
        pdf_validation_timeout_seconds=settings.pdf_validation_timeout_seconds,
        pdf_validation_cpu_seconds=settings.pdf_validation_cpu_seconds,
        pdf_validation_memory_bytes=settings.pdf_validation_memory_bytes,
    )
    storage_service.prepare()
    await database.open()
    app.state.database = database
    app.state.settings = settings
    app.state.storage_service = storage_service
    app.state.qrcode_service = QRCodeService(
        settings.storage_root,
        str(settings.public_base_url),
    )
    app.state.public_miss_limiter = PublicMissLimiter(
        settings.public_miss_limit,
        settings.public_miss_window_seconds,
    )
    try:
        yield
    finally:
        await database.close()


def create_app() -> FastAPI:
    """Create the Phase 1-B local-only business-loop application."""

    application = FastAPI(
        title="Product PDF QR",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(UploadRequestLimitMiddleware)
    register_exception_handlers(application)
    application.include_router(admin_router)
    application.include_router(product_router)
    application.include_router(qrcode_router)
    application.include_router(storage_router)
    application.include_router(public_router)

    @application.get("/health/live", include_in_schema=False)
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready", include_in_schema=False)
    async def readiness(request: Request) -> JSONResponse:
        database = cast(Database, request.app.state.database)
        ready = await database.is_ready()
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ok" if ready else "unavailable"},
        )

    return application


app = create_app()
