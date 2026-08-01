"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from product_pdf_qr.config import get_settings
from product_pdf_qr.database import Database
from product_pdf_qr.errors import register_exception_handlers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop infrastructure owned by the application process."""

    database = Database(get_settings())
    await database.open()
    app.state.database = database
    try:
        yield
    finally:
        await database.close()


def create_app() -> FastAPI:
    """Create the infrastructure-only Phase 1-A application."""

    application = FastAPI(
        title="Product PDF QR",
        version="0.1.0",
        lifespan=lifespan,
    )
    register_exception_handlers(application)

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
