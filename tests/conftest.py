"""Shared synthetic configuration for tests."""

from __future__ import annotations

import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://app_rw:synthetic-only@127.0.0.1:5432/product_pdf_qr_test",
)
