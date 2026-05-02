"""PDF attachment handling in the Anthropic provider."""
from __future__ import annotations

import base64
import importlib.util
import logging
from pathlib import Path

# Anthropic provider is a plugin — load via the same pattern as
# tests/test_cli_ui_image_paste.py.
PROVIDER_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "anthropic-provider" / "provider.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_anthropic_provider_pdf", PROVIDER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_minimal_pdf(num_pages: int = 1) -> bytes:
    pages = b"<< /Type /Page >>\n" * num_pages
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        + pages
        + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


def test_anthropic_builds_document_block_for_pdf_attachment(tmp_path):
    module = _load_provider_module()
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(_make_minimal_pdf(num_pages=2))

    blocks = module._content_blocks_with_attachments(
        text="Summarize this PDF.",
        attachment_paths=[str(pdf_path)],
    )

    # Expect: 1 document block + 1 text block, in that order
    assert len(blocks) == 2
    assert blocks[0]["type"] == "document"
    assert blocks[0]["source"]["type"] == "base64"
    assert blocks[0]["source"]["media_type"] == "application/pdf"
    decoded = base64.standard_b64decode(blocks[0]["source"]["data"])
    assert decoded == pdf_path.read_bytes()
    assert blocks[1]["type"] == "text"
    assert blocks[1]["text"] == "Summarize this PDF."


def test_anthropic_skips_oversize_pdf(tmp_path, caplog):
    from plugin_sdk.pdf_helpers import PDF_MAX_BYTES

    module = _load_provider_module()
    pdf_path = tmp_path / "big.pdf"
    pdf_path.write_bytes(b"x" * (PDF_MAX_BYTES + 1))

    with caplog.at_level(logging.WARNING):
        blocks = module._content_blocks_with_attachments(
            text="hi",
            attachment_paths=[str(pdf_path)],
        )

    # Only text block; PDF dropped with warning
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert any("over 32 MB" in r.message for r in caplog.records)


def test_anthropic_skips_unreadable_pdf(tmp_path, caplog):
    module = _load_provider_module()

    with caplog.at_level(logging.WARNING):
        blocks = module._content_blocks_with_attachments(
            text="hi",
            attachment_paths=[str(tmp_path / "missing.pdf")],
        )

    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert any("unreadable" in r.message for r in caplog.records)


def test_anthropic_image_path_still_works(tmp_path):
    """Regression: existing image attachment path is unchanged."""
    module = _load_provider_module()
    # Minimal valid PNG (1x1 transparent)
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c63000100000005000100020701020000000049454e"
        "44ae426082"
    )
    img = tmp_path / "tiny.png"
    img.write_bytes(png_bytes)

    blocks = module._content_blocks_with_attachments(
        text="Describe this.",
        attachment_paths=[str(img)],
    )
    assert any(b["type"] == "image" for b in blocks)
    assert blocks[-1]["type"] == "text"
