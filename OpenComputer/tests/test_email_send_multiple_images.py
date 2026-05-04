"""Tests for EmailAdapter.send_multiple_images MIME multi-attachment.

Wave 5 T11 final closure (Hermes-port 3de8e2168). Single SMTP send for
all images via stdlib EmailMessage.add_attachment().
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _load_adapter():
    p = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "email"
        / "adapter.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_email_adapter_for_T11", str(p)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def email_adapter_class():
    return _load_adapter().EmailAdapter


def _make_stub_adapter(cls):
    a = cls.__new__(cls)
    a._from_address = "agent@example.com"
    a._smtp_send = MagicMock()
    return a


@pytest.mark.asyncio
async def test_empty_list_is_noop(email_adapter_class):
    a = _make_stub_adapter(email_adapter_class)
    await a.send_multiple_images("rcpt@example.com", [])
    a._smtp_send.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_recipient_is_noop(email_adapter_class):
    a = _make_stub_adapter(email_adapter_class)
    await a.send_multiple_images("not-an-email", ["/a.png"])
    a._smtp_send.assert_not_called()


@pytest.mark.asyncio
async def test_one_smtp_send_for_n_images(tmp_path, email_adapter_class):
    a = _make_stub_adapter(email_adapter_class)
    paths = []
    for i in range(3):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"\x89PNG")
        paths.append(str(p))
    await a.send_multiple_images("rcpt@example.com", paths, caption="batch")
    assert a._smtp_send.call_count == 1
    msg = a._smtp_send.call_args.args[0]
    # Walk the MIME structure to count attachments
    parts = list(msg.walk())
    image_parts = [p for p in parts if p.get_content_maintype() == "image"]
    assert len(image_parts) == 3


@pytest.mark.asyncio
async def test_missing_files_skipped(tmp_path, email_adapter_class):
    a = _make_stub_adapter(email_adapter_class)
    real = tmp_path / "real.png"
    real.write_bytes(b"\x89PNG")
    paths = [str(tmp_path / "missing.png"), str(real)]
    await a.send_multiple_images("rcpt@example.com", paths)
    assert a._smtp_send.call_count == 1
    msg = a._smtp_send.call_args.args[0]
    image_parts = [p for p in msg.walk() if p.get_content_maintype() == "image"]
    assert len(image_parts) == 1


@pytest.mark.asyncio
async def test_no_attachments_means_no_send(tmp_path, email_adapter_class):
    """If every file is missing, no SMTP call."""
    a = _make_stub_adapter(email_adapter_class)
    await a.send_multiple_images(
        "rcpt@example.com", [str(tmp_path / "ghost.png")],
    )
    a._smtp_send.assert_not_called()


@pytest.mark.asyncio
async def test_subject_and_caption_carried(tmp_path, email_adapter_class):
    a = _make_stub_adapter(email_adapter_class)
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG")
    await a.send_multiple_images(
        "rcpt@example.com", [str(p)],
        caption="custom caption", subject="Custom Subject",
    )
    msg = a._smtp_send.call_args.args[0]
    assert msg["Subject"] == "Custom Subject"
    body = msg.get_body()
    assert body is not None
    assert "custom caption" in body.get_content()


@pytest.mark.asyncio
async def test_smtp_error_swallowed(tmp_path, email_adapter_class):
    """SMTP delivery failure logs but doesn't raise."""
    a = _make_stub_adapter(email_adapter_class)
    a._smtp_send = MagicMock(side_effect=RuntimeError("smtp down"))
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG")
    # Must not raise
    await a.send_multiple_images("rcpt@example.com", [str(p)])
    assert a._smtp_send.call_count == 1
