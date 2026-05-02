"""OpenAI provider: PDF attachments dropped with warning (no native support)."""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

PROVIDER_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "openai-provider" / "provider.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_openai_provider", PROVIDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_minimal_pdf() -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        b"<< /Type /Page >>\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


def test_openai_warns_and_drops_pdf_attachment(tmp_path, caplog):
    """OpenAI doesn't natively support PDFs; should warn + drop."""
    module = _load_provider_module()
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(_make_minimal_pdf())

    with caplog.at_level(logging.WARNING):
        blocks = module._content_blocks_with_attachments(
            text="What is in the document?",
            attachment_paths=[str(pdf_path)],
        )

    # Text still goes through; no PDF block (OpenAI has no native PDF type)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == "What is in the document?"
    assert any(
        "PDF" in r.message and ("not support" in r.message or "drop" in r.message.lower())
        for r in caplog.records
    )


def test_openai_image_path_still_works(tmp_path):
    """Regression: existing image attachment path is unchanged."""
    module = _load_provider_module()
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
    image_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(image_blocks) == 1
    text_blocks = [b for b in blocks if b.get("type") == "text"]
    assert len(text_blocks) == 1
