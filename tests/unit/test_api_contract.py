"""Phase 1-B OpenAPI surface contract."""

from __future__ import annotations

from product_pdf_qr.main import create_app


def test_business_loop_routes_are_present_and_out_of_scope_routes_absent() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]

    assert "/api/products" in paths
    assert set(paths["/api/products"]) == {"get", "post"}
    assert "/api/products/{product_id}" in paths
    assert set(paths["/api/products/{product_id}"]) == {"get"}
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


def test_product_create_and_read_contracts_include_persisted_name() -> None:
    schema = create_app().openapi()
    create_schema = schema["components"]["schemas"]["ProductCreateRequest"]
    detail_schema = schema["components"]["schemas"]["ProductDetailResponse"]

    assert set(create_schema["required"]) == {"code", "name"}
    assert create_schema["properties"]["name"]["maxLength"] == 120
    assert "name" in detail_schema["properties"]
    assert {"created_at", "updated_at", "pdf_status", "qrcode_status"}.issubset(
        detail_schema["required"]
    )


def test_pdf_upload_no_longer_accepts_a_client_actor_identity() -> None:
    schema = create_app().openapi()
    operation = schema["paths"]["/api/products/{product_id}/pdf"]["post"]
    body_reference = operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    body_schema = schema["components"]["schemas"][body_reference.rsplit("/", 1)[-1]]

    assert "actor_id" not in str(operation)
    assert body_schema["required"] == ["file"]
    assert set(body_schema["properties"]) == {"file"}
