"""QR-code domain."""

from product_pdf_qr.domains.qrcode.service import (
    QRCodeGenerationError,
    QRCodeResult,
    QRCodeService,
)

__all__ = ["QRCodeGenerationError", "QRCodeResult", "QRCodeService"]
