"""Tests for plugin_sdk.pdf_helpers — PDF byte handling utilities."""
from __future__ import annotations

import base64

from plugin_sdk.pdf_helpers import (
    PDF_HARD_PAGE_LIMIT,
    PDF_MAX_BYTES,
    PDF_SOFT_PAGE_LIMIT,
    count_pdf_pages,
    pdf_to_base64,
)


def _make_minimal_pdf(num_pages: int = 1) -> bytes:
    """Build a minimal-but-parseable PDF with the requested page count."""
    pages = b""
    for _ in range(num_pages):
        pages += b"<< /Type /Page >>\n"
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        + pages
        + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


def test_constants_match_spec():
    assert PDF_MAX_BYTES == 32 * 1024 * 1024
    assert PDF_HARD_PAGE_LIMIT == 600
    assert PDF_SOFT_PAGE_LIMIT == 100


def test_count_pdf_pages_known_3_page_pdf():
    pdf_bytes = _make_minimal_pdf(num_pages=3)
    assert count_pdf_pages(pdf_bytes) == 3


def test_count_pdf_pages_returns_zero_for_garbage():
    assert count_pdf_pages(b"not a pdf at all") == 0


def test_pdf_to_base64_roundtrip():
    pdf_bytes = _make_minimal_pdf(num_pages=1)
    encoded = pdf_to_base64(pdf_bytes)
    assert isinstance(encoded, str)
    assert base64.standard_b64decode(encoded) == pdf_bytes
