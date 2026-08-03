"""Append-only audit domain."""

from product_pdf_qr.domains.audit.service import (
    AuditEvent,
    append_event,
    append_independent_event,
)

__all__ = ["AuditEvent", "append_event", "append_independent_event"]
