"""Local-only management endpoints for the Phase 1-B business loop."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from pydantic import BaseModel, Field

from product_pdf_qr.database import Database
from product_pdf_qr.dependencies import (
    get_database,
    get_qrcode_service,
    get_storage_service,
)
from product_pdf_qr.domains.audit import AuditEvent, append_independent_event
from product_pdf_qr.domains.product.service import Product, create_product
from product_pdf_qr.domains.qrcode import QRCodeGenerationError, QRCodeResult, QRCodeService
from product_pdf_qr.domains.storage import StorageService, UploadRejected
from product_pdf_qr.domains.version import record_upload_rejection, upload_pdf
from product_pdf_qr.errors import AppError

router = APIRouter(prefix="/api/products", tags=["products"])


class ProductCreateRequest(BaseModel):
    """Product creation input."""

    code: str = Field(min_length=1)


class ProductCreateResponse(BaseModel):
    """Product creation output for the local management client."""

    id: int
    code: str
    public_token: str
    status: str
    current_version_id: int | None
    public_url: str
    qrcode_url: str
    qrcode_status: str


class PDFUploadResponse(BaseModel):
    """Successful append-only upload output."""

    version_id: int
    version_no: int
    sha256: str
    size_bytes: int
    original_filename: str


class QRCodeRetryResponse(BaseModel):
    """Manual QR retry result."""

    status: str
    cache_hit: bool


async def _record_qrcode_outcome(
    database: Database,
    product: Product,
    *,
    action: str,
    result: str,
    reason: str | None = None,
) -> None:
    detail: dict[str, object] | None = {"reason": reason} if reason is not None else None
    await append_independent_event(
        database,
        AuditEvent(
            action=action,
            result="success" if result == "success" else "failure",
            target_type="product",
            target_id=product.id,
            product_code=product.code,
            detail=detail,
        ),
    )


async def _load_product(database: Database, product_id: int) -> Product:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT id, code, public_token, status, current_version_id
            FROM products
            WHERE id = %s
            """,
            (product_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise AppError("product_not_found", "产品不存在。", 404)
    return Product(
        id=cast(int, row["id"]),
        code=str(row["code"]),
        public_token=str(row["public_token"]),
        status=str(row["status"]),
        current_version_id=(
            cast(int, row["current_version_id"]) if row["current_version_id"] is not None else None
        ),
    )


async def _generate_with_audit(
    database: Database,
    qrcode_service: QRCodeService,
    product: Product,
) -> QRCodeResult:
    try:
        result = await qrcode_service.get_or_generate(product.code, product.public_token)
    except QRCodeGenerationError as error:
        await _record_qrcode_outcome(
            database,
            product,
            action="qrcode_generation_failure",
            result="failure",
            reason=error.code,
        )
        raise
    if result.cache_error is not None:
        await _record_qrcode_outcome(
            database,
            product,
            action="qrcode_cache_failure",
            result="failure",
            reason=result.cache_error,
        )
    return result


@router.post(
    "",
    response_model=ProductCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建产品",
)
async def create_product_endpoint(
    payload: ProductCreateRequest,
    database: Annotated[Database, Depends(get_database)],
    qrcode_service: Annotated[QRCodeService, Depends(get_qrcode_service)],
) -> ProductCreateResponse:
    """Create first, commit, then pre-generate the derived QR cache best-effort."""

    product = await create_product(database, payload.code, request_id=uuid4())
    qrcode_status = "ready"
    try:
        result = await _generate_with_audit(database, qrcode_service, product)
        if result.cache_error is not None:
            qrcode_status = "ready_cache_degraded"
    except QRCodeGenerationError:
        qrcode_status = "generation_failed"
    return ProductCreateResponse(
        id=product.id,
        code=product.code,
        public_token=product.public_token,
        status=product.status,
        current_version_id=product.current_version_id,
        public_url=qrcode_service.public_url(product.public_token),
        qrcode_url=f"/api/products/{product.id}/qrcode",
        qrcode_status=qrcode_status,
    )


@router.get("/{product_id}/qrcode", summary="下载二维码")
async def download_qrcode(
    product_id: int,
    database: Annotated[Database, Depends(get_database)],
    qrcode_service: Annotated[QRCodeService, Depends(get_qrcode_service)],
) -> Response:
    """Download one QR PNG using the exact normalized code as its filename."""

    product = await _load_product(database, product_id)
    result = await _generate_with_audit(database, qrcode_service, product)
    return Response(
        content=result.image_bytes,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{product.code}.png"'},
    )


@router.post(
    "/{product_id}/qrcode/retry",
    response_model=QRCodeRetryResponse,
    summary="重试生成二维码",
)
async def retry_qrcode(
    product_id: int,
    database: Annotated[Database, Depends(get_database)],
    qrcode_service: Annotated[QRCodeService, Depends(get_qrcode_service)],
) -> QRCodeRetryResponse:
    """Manually retry deterministic generation without changing product state."""

    product = await _load_product(database, product_id)
    result = await _generate_with_audit(database, qrcode_service, product)
    await _record_qrcode_outcome(
        database,
        product,
        action="qrcode_generation_recovered",
        result="success",
    )
    return QRCodeRetryResponse(status="ready", cache_hit=result.cache_hit)


@router.post(
    "/{product_id}/pdf",
    response_model=PDFUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="上传PDF",
)
async def upload_pdf_endpoint(
    product_id: int,
    actor_id: Annotated[int, Form(gt=0)],
    file: Annotated[UploadFile, File()],
    database: Annotated[Database, Depends(get_database)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
) -> PDFUploadResponse:
    """Validate outside the lock, then serialize current-version mutation."""

    request_id = uuid4()
    try:
        validated = await storage.receive_and_validate(file)
    except UploadRejected as error:
        await record_upload_rejection(
            database,
            product_id=product_id,
            actor_id=actor_id,
            reason=error.code,
            stage=error.stage,
            request_id=request_id,
        )
        raise
    version = await upload_pdf(
        database,
        storage,
        product_id=product_id,
        actor_id=actor_id,
        upload=validated,
        request_id=request_id,
    )
    return PDFUploadResponse(
        version_id=version.id,
        version_no=version.version_no,
        sha256=version.sha256,
        size_bytes=version.size_bytes,
        original_filename=version.original_filename,
    )
