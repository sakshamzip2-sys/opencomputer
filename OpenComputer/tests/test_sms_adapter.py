"""Tests for the SMS / Twilio channel adapter.

Mocked aiohttp: we don't actually hit Twilio's API or open a real
TCP socket. Verifies the adapter logic — config parsing, signature
validation, message chunking, markdown stripping, redaction —
without external dependencies.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugin_sdk.core import Platform


def _load_adapter_module():
    """Load the SMS adapter module via importlib because it lives at a
    hyphenated path that's not a Python package."""
    spec = importlib.util.spec_from_file_location(
        "_sms_adapter",
        str(Path(__file__).parent.parent / "extensions" / "sms" / "adapter.py"),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_adapter_module()
SmsAdapter = _mod.SmsAdapter


def _make_adapter(**overrides):
    config = {
        "account_sid": "ACtest",
        "auth_token": "secret_token_test",
        "from_number": "+15551234567",
        "webhook_port": 8080,
        "webhook_host": "127.0.0.1",
        "webhook_url": "https://example.com/webhooks/twilio",
        "insecure_no_signature": False,
    }
    config.update(overrides)
    return SmsAdapter(config=config)


# ── platform + capability shape ───────────────────────────────────────


def test_platform_is_sms():
    a = _make_adapter()
    assert a.platform == Platform.SMS


def test_max_message_length_matches_sms_cap():
    a = _make_adapter()
    assert a.max_message_length == 1600


# ── markdown stripping ────────────────────────────────────────────────


def test_strip_markdown_basic():
    out = _mod._strip_markdown("**bold** and _italic_ and `code`")
    assert "**" not in out
    assert "_" not in out
    assert "`" not in out
    assert "bold" in out
    assert "italic" in out
    assert "code" in out


def test_strip_markdown_links():
    # PR 3c.3: switched to plugin_sdk.channel_helpers.strip_markdown,
    # which (matching every other channel adapter now) drops the URL
    # rather than rendering it inline. Rationale: URLs in SMS look
    # ugly twice over — once in the link text, once in parentheses.
    out = _mod._strip_markdown("see [here](https://example.com)")
    assert "[" not in out
    assert "]" not in out
    assert "(" not in out
    assert ")" not in out
    assert "here" in out


def test_strip_markdown_headers():
    out = _mod._strip_markdown("# Title\n\nbody text")
    assert "#" not in out
    assert "Title" in out
    assert "body text" in out


def test_strip_markdown_fenced_code_block():
    # PR 3c.3: shared helper preserves fenced-code *content* (drops just
    # the fence markers + language tag). The legacy local stripper
    # removed the whole block — that loses information for SMS users
    # asking "explain this snippet". The new behaviour matches what
    # other text-only channels (signal, whatsapp) already do.
    out = _mod._strip_markdown("before\n```python\nhidden\n```\nafter")
    assert "```" not in out
    assert "hidden" in out  # body preserved
    assert "before" in out
    assert "after" in out


# ── phone number redaction ────────────────────────────────────────────


def test_redact_phone_keeps_country_code_and_last_four():
    # PR 3c.3: switched from local _redact_phone (kept last 2) to the
    # shared plugin_sdk.channel_helpers.redact_phone (keeps last 4).
    out = _mod._redact_phone("+15551234567")
    assert out.startswith("+1")
    assert out.endswith("4567")
    assert "12345" not in out
    assert "555" not in out


def test_redact_phone_handles_short_input():
    # The shared helper returns "" for falsy phones (vs "***" in the
    # legacy local implementation) and "***" only for short non-empty
    # strings without enough digits to keep meaningful tail.
    assert _mod._redact_phone("") == ""
    assert _mod._redact_phone("123") == "***"
    assert _mod._redact_phone(None) == ""


# ── chunk for SMS ─────────────────────────────────────────────────────


def test_short_message_not_chunked():
    a = _make_adapter()
    chunks = a._chunk_for_sms("hello world")
    assert chunks == ["hello world"]


def test_long_message_chunks_on_lines():
    a = _make_adapter()
    text = "\n".join(["line " + str(i) for i in range(500)])
    chunks = a._chunk_for_sms(text, limit=200)
    assert len(chunks) > 1
    # Chunks each fit the limit (or hard-split a single oversized line)
    for c in chunks:
        # Single hard-split lines may equal limit exactly
        assert len(c) <= 200 or "line" not in c


def test_oversized_single_line_hard_splits():
    a = _make_adapter()
    text = "x" * 5000  # one line, way over limit
    chunks = a._chunk_for_sms(text, limit=1000)
    assert len(chunks) == 5
    for c in chunks:
        assert len(c) == 1000


# ── Twilio signature validation ───────────────────────────────────────


def test_signature_validates_correct_hmac():
    """Compute a Twilio signature from scratch and verify validation passes.

    The Twilio algorithm: HMAC-SHA1 of (URL + sorted concat of form
    key/value pairs) keyed on the auth token, base64-encoded.
    """
    import base64
    import hashlib
    import hmac

    a = _make_adapter()
    url = "https://example.com/webhooks/twilio"
    params = {"From": "+15559999999", "Body": "hi"}

    data = url + "Body" + "hi" + "From" + "+15559999999"
    mac = hmac.new(
        a._auth_token.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha1,
    )
    sig = base64.b64encode(mac.digest()).decode("utf-8")

    assert a._validate_twilio_signature(url, params, sig) is True


def test_signature_rejects_wrong_hmac():
    a = _make_adapter()
    bad_sig = "AAAA_definitely_not_real"
    assert (
        a._validate_twilio_signature(
            "https://example.com/webhooks/twilio",
            {"From": "+1234567890", "Body": "hi"},
            bad_sig,
        )
        is False
    )


def test_port_variant_url_strips_default_port():
    out = SmsAdapter._port_variant_url("https://example.com:443/path")
    assert out == "https://example.com/path"


def test_port_variant_url_adds_default_port():
    out = SmsAdapter._port_variant_url("https://example.com/path")
    assert out is not None
    assert ":443" in out


def test_port_variant_url_no_change_for_nonstandard_port():
    out = SmsAdapter._port_variant_url("https://example.com:8080/path")
    assert out is None


# ── connect refuses when SMS_WEBHOOK_URL missing in production mode ───


@pytest.mark.asyncio
async def test_connect_refuses_without_webhook_url_or_insecure_flag():
    a = _make_adapter(webhook_url="", insecure_no_signature=False)
    ok = await a.connect()
    assert ok is False


@pytest.mark.asyncio
async def test_connect_refuses_when_no_from_number():
    a = _make_adapter(from_number="")
    ok = await a.connect()
    assert ok is False


# ── send (mocked Twilio REST) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_calls_twilio_with_basic_auth():
    a = _make_adapter()

    # Mock the persistent http session
    fake_resp = AsyncMock()
    fake_resp.__aenter__.return_value.status = 201
    fake_resp.__aenter__.return_value.json = AsyncMock(
        return_value={"sid": "SMfake123"}
    )
    fake_session = MagicMock()
    fake_session.post = MagicMock(return_value=fake_resp)
    a._http_session = fake_session

    result = await a.send("+19998887777", "hello world")
    assert result.success is True
    assert result.message_id == "SMfake123"

    fake_session.post.assert_called_once()
    call_args = fake_session.post.call_args
    # Authorization header is HTTP Basic
    assert call_args.kwargs["headers"]["Authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_send_returns_failure_on_4xx():
    a = _make_adapter()

    fake_resp = AsyncMock()
    fake_resp.__aenter__.return_value.status = 400
    fake_resp.__aenter__.return_value.json = AsyncMock(
        return_value={"message": "Invalid 'To' phone number"}
    )
    fake_session = MagicMock()
    fake_session.post = MagicMock(return_value=fake_resp)
    a._http_session = fake_session

    result = await a.send("+1bad", "hello")
    assert result.success is False
    assert "Invalid" in (result.error or "")
