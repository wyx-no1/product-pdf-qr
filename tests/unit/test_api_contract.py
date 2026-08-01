"""Phase 1-B OpenAPI surface contract."""

from __future__ import annotations

from product_pdf_qr.main import create_app


def test_business_loop_routes_are_present_and_out_of_scope_routes_absent() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]

    assert "/api/products" in paths
    assert "/api/products/{product_id}/pdf" in paths
    assert "/api/products/{product_id}/qrcode" in paths
    assert "/api/products/{product_id}/qrcode/retry" in paths
    assert "/api/storage/orphans" in paths
    assert "/api/qrcode/failures" in paths
    assert "/p/{public_token}" in paths

    serialized = str(schema).lower()
    assert "/login" not in serialized
    assert "/import" not in serialized
    assert "/delete" not in serialized
    assert "/restore" not in serialized
    assert "/disable" not in serialized
