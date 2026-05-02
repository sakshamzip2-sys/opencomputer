"""PDF byte-handling utilities shared across provider plugins.

Provides:

- Size + page-count limits matching Anthropic's published PDF support spec
  (https://docs.claude.com/en/build-with-claude/pdf-support).
- A cheap byte-counting page-count heuristic that avoids adding ``pypdf``
  as a dependency. Approximate; intended for guard rails, not analysis.
- Base64 encoding helper for the Anthropic content-block source format.

These helpers are deliberately stateless and side-effect-free so each
provider plugin can compose them into its own attachment-handling path
(see ``extensions/anthropic-provider/provider.py`` and the SP2 design
doc at ``docs/superpowers/specs/2026-05-02-sp2-pdf-provider-hardening-design.md``).
"""
from __future__ import annotations

import base64

# Anthropic limits (per https://docs.claude.com/en/build-with-claude/pdf-support):
# - Max request size: 32 MB (entire payload, including non-PDF content)
# - Max pages: 600 (hard cap)
# - Effective max for 200k-context-window models: 100 pages
PDF_MAX_BYTES: int = 32 * 1024 * 1024
PDF_HARD_PAGE_LIMIT: int = 600
PDF_SOFT_PAGE_LIMIT: int = 100


def count_pdf_pages(data: bytes) -> int:
    """Count PDF pages via byte-marker scan.

    PDF objects of type ``/Page`` are individual pages. ``/Pages`` is the
    catalog (parent). The page count is the number of ``/Type /Page``
    markers minus the number of ``/Type /Pages`` catalogs.

    Approximate — a content stream containing the literal bytes
    ``/Type /Page`` could over-count. Good enough for the soft/hard
    limit guards (we want to catch obvious cases, not produce a precise
    count).

    Returns 0 for non-PDF input or malformed data.
    """
    page_marker = data.count(b"/Type /Page") - data.count(b"/Type /Pages")
    return max(page_marker, 0)


def pdf_to_base64(data: bytes) -> str:
    """Standard-base64 encode PDF bytes for Anthropic's document source field."""
    return base64.standard_b64encode(data).decode("ascii")


__all__ = [
    "PDF_MAX_BYTES",
    "PDF_HARD_PAGE_LIMIT",
    "PDF_SOFT_PAGE_LIMIT",
    "count_pdf_pages",
    "pdf_to_base64",
]
