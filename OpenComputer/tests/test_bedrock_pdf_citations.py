"""Bedrock provider: PDF document blocks + citations footgun fix."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TRANSPORT_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "aws-bedrock-provider" / "transport.py"
)


def _load_transport_module():
    spec = importlib.util.spec_from_file_location(
        "_test_bedrock_transport", TRANSPORT_PATH
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


def _make_request(*, text: str, attachments: list[str] | None = None):
    """Build a NormalizedRequest with one user message."""
    from plugin_sdk.core import Message
    from plugin_sdk.transports import NormalizedRequest

    return NormalizedRequest(
        model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        messages=[Message(role="user", content=text, attachments=attachments or [])],
        system="",
        max_tokens=1024,
        temperature=0.0,
        tools=[],
    )


@pytest.fixture
def transport():
    """Construct a BedrockTransport bypassing __init__ (we never call .send,
    so we don't need a real boto3 client)."""
    module = _load_transport_module()
    t = module.BedrockTransport.__new__(module.BedrockTransport)
    t._region = "us-east-1"
    t._client = None  # format_request never touches the client
    return t


def test_bedrock_no_citations_when_text_only(transport, tmp_path):
    """Regression: text-only requests must NOT set citations."""
    req = _make_request(text="What is the capital of France?")
    native = transport.format_request(req)
    assert "additionalModelRequestFields" not in native or \
        "citations" not in native.get("additionalModelRequestFields", {})


def test_bedrock_sets_citations_enabled_when_pdf_present(transport, tmp_path):
    """THE FOOTGUN FIX: PDFs must auto-enable citations to avoid silent text-only fallback."""
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(_make_minimal_pdf())

    req = _make_request(text="Summarize", attachments=[str(pdf_path)])
    native = transport.format_request(req)

    assert native["additionalModelRequestFields"]["citations"]["enabled"] is True


def test_bedrock_builds_document_block_with_raw_bytes(transport, tmp_path):
    """Bedrock Converse documentBlock format requires raw bytes (not base64)."""
    pdf_path = tmp_path / "doc.pdf"
    pdf_bytes = _make_minimal_pdf()
    pdf_path.write_bytes(pdf_bytes)

    req = _make_request(text="Summarize", attachments=[str(pdf_path)])
    native = transport.format_request(req)

    user_msg = native["messages"][0]
    assert user_msg["role"] == "user"
    doc_blocks = [b for b in user_msg["content"] if "document" in b]
    assert len(doc_blocks) == 1
    doc = doc_blocks[0]["document"]
    assert doc["format"] == "pdf"
    assert doc["source"]["bytes"] == pdf_bytes  # RAW bytes, not base64


def test_bedrock_skips_oversize_pdf(transport, tmp_path, caplog):
    """Oversize PDF dropped with warning; no document block, no citations."""
    import logging

    from plugin_sdk.pdf_helpers import PDF_MAX_BYTES

    pdf_path = tmp_path / "huge.pdf"
    pdf_path.write_bytes(b"x" * (PDF_MAX_BYTES + 1))

    req = _make_request(text="Summarize", attachments=[str(pdf_path)])
    with caplog.at_level(logging.WARNING):
        native = transport.format_request(req)

    user_msg = native["messages"][0]
    assert all("document" not in b for b in user_msg["content"])
    assert "additionalModelRequestFields" not in native or \
        "citations" not in native.get("additionalModelRequestFields", {})
    assert any("over 32 MB" in r.message for r in caplog.records)
