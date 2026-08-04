"""Product domain."""

from product_pdf_qr.domains.product.service import (
    PRODUCT_NAME_MAX_LENGTH,
    Product,
    create_product,
    generate_public_token,
    get_product,
    is_normalized_product_code,
    list_products,
    normalize_product_code,
    normalize_product_name,
)

__all__ = [
    "PRODUCT_NAME_MAX_LENGTH",
    "Product",
    "create_product",
    "generate_public_token",
    "get_product",
    "is_normalized_product_code",
    "list_products",
    "normalize_product_code",
    "normalize_product_name",
]
