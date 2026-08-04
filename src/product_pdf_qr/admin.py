"""Server-rendered shell for the minimal administration page."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/admin", include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@router.get("", response_class=HTMLResponse)
@router.get("/products/{product_id}", response_class=HTMLResponse)
async def admin_page(request: Request, product_id: int | None = None) -> HTMLResponse:
    """Render the client-side admin shell without loading product data."""

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"product_id": product_id},
    )
