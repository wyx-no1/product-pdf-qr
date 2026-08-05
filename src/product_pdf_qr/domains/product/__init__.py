"""Product domain."""

from product_pdf_qr.domains.product.service import (
    PRODUCT_NAME_MAX_LENGTH,
    Product,
    ProductPDFStatus,
    ProductStatus,
    create_product,
    generate_public_token,
    get_product,
    is_normalized_product_code,
    list_products,
    normalize_product_code,
    normalize_product_name,
    set_product_status,
)

__all__ = [
    "PRODUCT_NAME_MAX_LENGTH",
    "Product",
    "ProductPDFStatus",
    "ProductStatus",
    "create_product",
    "generate_public_token",
    "get_product",
    "is_normalized_product_code",
    "list_products",
    "normalize_product_code",
    "normalize_product_name",
    "set_product_status",
]
