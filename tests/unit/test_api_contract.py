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


def test_openapi_title_and_route_summaries_are_localized() -> None:
    schema = create_app().openapi()

    assert schema["info"]["title"] == "产品PDF二维码系统"
    assert schema["paths"]["/api/products"]["post"]["summary"] == "创建产品"
    assert schema["paths"]["/api/products/{product_id}/pdf"]["post"]["summary"] == "上传PDF"
    assert schema["paths"]["/api/products/{product_id}/qrcode"]["get"]["summary"] == "下载二维码"
    assert (
        schema["paths"]["/api/products/{product_id}/qrcode/retry"]["post"]["summary"]
        == "重试生成二维码"
    )
    assert schema["paths"]["/p/{public_token}"]["get"]["summary"] == "公开扫码访问"
    assert schema["paths"]["/api/qrcode/failures"]["get"]["summary"] == "查看二维码失败记录"
    assert schema["paths"]["/api/storage/orphans"]["get"]["summary"] == "查看孤儿文件记录"
