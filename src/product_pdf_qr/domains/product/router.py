"""Local-only management endpoints for the Phase 1-B business loop."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from pydantic import BaseModel, Field

from product_pdf_qr.database import Database
from product_pdf_qr.dependencies import (
    get_current_admin,
    get_database,
    get_qrcode_service,
    get_storage_service,
)
from product_pdf_qr.domains.audit import AuditEvent, append_independent_event
from product_pdf_qr.domains.auth import AuthenticatedAdmin
from product_pdf_qr.domains.product.service import (
    PRODUCT_NAME_MAX_LENGTH,
    Product,
    ProductPDFStatus,
    create_product,
    get_product,
    list_products,
)
from product_pdf_qr.domains.qrcode import QRCodeGenerationError, QRCodeResult, QRCodeService
from product_pdf_qr.domains.storage import StorageService, UploadRejected
from product_pdf_qr.domains.version import record_upload_rejection, upload_pdf

router = APIRouter(prefix="/api/products", tags=["products"])


class ProductCreateRequest(BaseModel):
    """Product creation input."""

    code: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH)


class ProductCreateResponse(BaseModel):
    """Product creation output for the local management client."""

    id: int
    code: str
    name: str
    public_token: str
    status: str
    current_version_id: int | None
    public_url: str
    qrcode_url: str
    qrcode_status: str


class ProductListItemResponse(BaseModel):
    """One persisted product shown in the administration list."""

    id: int
    code: str
    name: str | None
    pdf_status: ProductPDFStatus
    updated_at: datetime


class ProductDetailResponse(BaseModel):
    """Complete persisted product state for a reloadable detail page."""

    id: int
    code: str
    name: str | None
    status: str
    public_token: str
    public_url: str
    qrcode_url: str
    qrcode_status: str
    pdf_status: ProductPDFStatus
    current_version_id: int | None
    created_at: datetime
    updated_at: datetime


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
    return await get_product(database, product_id)


def _pdf_status(product: Product) -> ProductPDFStatus:
    return "uploaded" if product.current_version_id is not None else "not_uploaded"


def _required_timestamp(value: datetime | None) -> datetime:
    if value is None:
        raise RuntimeError("Persisted product is missing timestamps")
    return value


def _qrcode_status(qrcode_service: QRCodeService, product: Product) -> str:
    cache_path = qrcode_service.cache_root / f"{product.code}.png"
    return "ready" if cache_path.is_file() else "not_generated"


def _detail_response(
    product: Product,
    qrcode_service: QRCodeService,
) -> ProductDetailResponse:
    return ProductDetailResponse(
        id=product.id,
        code=product.code,
        name=product.name,
        status=product.status,
        public_token=product.public_token,
        public_url=qrcode_service.public_url(product.public_token),
        qrcode_url=f"/api/products/{product.id}/qrcode",
        qrcode_status=_qrcode_status(qrcode_service, product),
        pdf_status=_pdf_status(product),
        current_version_id=product.current_version_id,
        created_at=_required_timestamp(product.created_at),
        updated_at=_required_timestamp(product.updated_at),
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


@router.post("", response_model=ProductCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_product_endpoint(
    payload: ProductCreateRequest,
    admin: Annotated[AuthenticatedAdmin, Depends(get_current_admin)],
    database: Annotated[Database, Depends(get_database)],
    qrcode_service: Annotated[QRCodeService, Depends(get_qrcode_service)],
) -> ProductCreateResponse:
    """Create first, commit, then pre-generate the derived QR cache best-effort."""

    product = await create_product(
        database,
        payload.code,
        payload.name,
        actor_id=admin.id,
        request_id=uuid4(),
    )
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
        name=product.name or payload.name.strip(),
        public_token=product.public_token,
        status=product.status,
        current_version_id=product.current_version_id,
        public_url=qrcode_service.public_url(product.public_token),
        qrcode_url=f"/api/products/{product.id}/qrcode",
        qrcode_status=qrcode_status,
    )


@router.get("", response_model=list[ProductListItemResponse])
async def list_products_endpoint(
    database: Annotated[Database, Depends(get_database)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str | None, Query(max_length=120)] = None,
    pdf_status: Annotated[ProductPDFStatus | None, Query()] = None,
) -> list[ProductListItemResponse]:
    """Return a database-filtered page of persisted products for the admin index."""

    products = await list_products(
        database,
        limit=limit,
        offset=offset,
        q=q,
        pdf_status=pdf_status,
    )
    return [
        ProductListItemResponse(
            id=product.id,
            code=product.code,
            name=product.name,
            pdf_status=_pdf_status(product),
            updated_at=_required_timestamp(product.updated_at),
        )
        for product in products
    ]


@router.get("/{product_id}", response_model=ProductDetailResponse)
async def get_product_endpoint(
    product_id: int,
    database: Annotated[Database, Depends(get_database)],
    qrcode_service: Annotated[QRCodeService, Depends(get_qrcode_service)],
) -> ProductDetailResponse:
    """Return persisted detail state so admin pages survive reloads."""

    product = await get_product(database, product_id)
    return _detail_response(product, qrcode_service)


@router.get("/{product_id}/qrcode")
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


@router.post("/{product_id}/qrcode/retry", response_model=QRCodeRetryResponse)
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
)
async def upload_pdf_endpoint(
    product_id: int,
    file: Annotated[UploadFile, File()],
    admin: Annotated[AuthenticatedAdmin, Depends(get_current_admin)],
    database: Annotated[Database, Depends(get_database)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
) -> PDFUploadResponse:
    """Use the authenticated administrator while serializing the version mutation."""

    request_id = uuid4()
    try:
        validated = await storage.receive_and_validate(file)
    except UploadRejected as error:
        await record_upload_rejection(
            database,
            product_id=product_id,
            actor_id=admin.id,
            reason=error.code,
            stage=error.stage,
            request_id=request_id,
        )
        raise
    version = await upload_pdf(
        database,
        storage,
        product_id=product_id,
        actor_id=admin.id,
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
