"""Product domain."""

from product_pdf_qr.domains.product.service import (
    Product,
    create_product,
    generate_public_token,
    is_normalized_product_code,
    normalize_product_code,
)

__all__ = [
    "Product",
    "create_product",
    "generate_public_token",
    "is_normalized_product_code",
    "normalize_product_code",
]
