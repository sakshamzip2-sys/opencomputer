"""SMS adapter phone-redaction + shared-helper migration (PR 3c.3).

PR 3c.3 swapped the SMS adapter's local ``_redact_phone`` /
``_strip_markdown`` for the shared :mod:`plugin_sdk.channel_helpers`
implementations (``redact_phone`` / ``strip_markdown``). This file
covers two invariants:

1. Every log line that includes a phone number runs it through
   ``redact_phone`` — the raw E.164 must never appear in caplog
   output for connect / inbound webhook / send / send-error paths.
2. Markdown stripping continues to work end-to-end: an outbound
   message with bold/italic/links is sent to Twilio with the
   markdown markers removed.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugin_sdk.core import Platform


def _load():
    spec = importlib.util.spec_from_file_location(
        "_sms_adapter_redaction_test",
        str(Path(__file__).parent.parent / "extensions" / "sms" / "adapter.py"),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load()
SmsAdapter = _mod.SmsAdapter

_FROM = "+15551234567"
_INBOUND_FROM = "+15557654321"


def _make(**overrides):
    config = {
        "account_sid": "ACtest",
        "auth_token": "secret",
        "from_number": _FROM,
        "webhook_port": 8080,
        "webhook_host": "127.0.0.1",
        "webhook_url": "https://example.com/webhooks/twilio",
        "insecure_no_signature": False,
    }
    config.update(overrides)
    return SmsAdapter(config=config)


def _assert_no_raw(records: list[logging.LogRecord], *phones: str) -> None:
    joined = "\n".join(r.getMessage() for r in records)
    for phone in phones:
        assert phone not in joined, (
            f"raw phone {phone} leaked into logs:\n{joined}"
        )


# ---------------------------------------------------------------------------
# 1. Connect log line redacts the configured from_number
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_log_redacts_from_number(caplog) -> None:
    """The Twilio "listening" log line interpolates ``from_number`` —
    must use the shared redactor, not the raw E.164."""
    a = _make()
    # Stub aiohttp + signature path so connect() succeeds without binding.
    fake_runner = MagicMock()
    fake_runner.setup = AsyncMock()
    fake_runner.cleanup = AsyncMock()
    fake_site = MagicMock()
    fake_site.start = AsyncMock()
    fake_site.stop = AsyncMock()

    with patch.object(_mod.web, "AppRunner", return_value=fake_runner), \
         patch.object(_mod.web, "TCPSite", return_value=fake_site), \
         patch.object(_mod.aiohttp, "ClientSession"), \
         caplog.at_level(logging.INFO, logger="opencomputer.ext.sms"):
        ok = await a.connect()
    assert ok
    _assert_no_raw(caplog.records, _FROM)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "+1***4567" in joined


# ---------------------------------------------------------------------------
# 2. Inbound webhook log redacts sender phone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_log_redacts_sender(caplog) -> None:
    a = _make(insecure_no_signature=True, webhook_url="")
    # Build a fake aiohttp request with a Twilio-style POST body. We
    # bypass signature validation by setting ``insecure_no_signature``.
    # URL-encode the leading '+' so the form parser keeps it (raw '+' in
    # x-www-form-urlencoded means a literal space — Twilio always sends
    # the '+' as %2B in production payloads).
    body = (
        f"From=%2B{_INBOUND_FROM[1:]}&Body=hello+world&MessageSid=SMtest"
    ).encode()

    request = MagicMock()
    request.read = AsyncMock(return_value=body)
    request.headers = {}

    # Stop dispatch from actually running (we only care about the log line).
    a._message_handler = AsyncMock(return_value=None)

    with caplog.at_level(logging.INFO, logger="opencomputer.ext.sms"):
        await a._handle_webhook(request)
    _assert_no_raw(caplog.records, _INBOUND_FROM)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "+1***4321" in joined


# ---------------------------------------------------------------------------
# 3. Send error path redacts target phone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_error_log_redacts_target(caplog) -> None:
    a = _make()

    class _Resp:
        status = 400

        async def json(self) -> dict:
            return {"message": "boom"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def post(self, url, data=None, headers=None):
            return _Resp()

        async def close(self):
            return None

    a._http_session = _Session()
    with caplog.at_level(logging.ERROR, logger="opencomputer.ext.sms"):
        res = await a.send(_INBOUND_FROM, "hi")
    assert not res.success
    _assert_no_raw(caplog.records, _INBOUND_FROM)


# ---------------------------------------------------------------------------
# 4. Markdown stripping still works end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_strips_markdown_before_post() -> None:
    """Bold / italic / links / code formatting must all be stripped on
    the wire — Twilio renders raw markdown literally on subscriber
    handsets, which looks broken."""
    a = _make()
    posted_bodies: list[str] = []

    class _Resp:
        status = 200

        async def json(self) -> dict:
            return {"sid": "SM123"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def post(self, url, data=None, headers=None):
            # FormData is opaque; reach into its private fields for the
            # 'Body' value. We only care that markdown markers are gone.
            for field in data._fields:
                # Each field is (info_dict, headers_dict, value)
                info = field[0]
                value = field[2]
                if info.get("name") == "Body":
                    posted_bodies.append(str(value))
            return _Resp()

        async def close(self):
            return None

    a._http_session = _Session()
    res = await a.send(
        "+15551112222", "**bold** and _italic_ and `code` and [text](http://x.y)"
    )
    assert res.success
    assert posted_bodies, "Body field never observed on outbound POST"
    body_sent = posted_bodies[0]
    # Markers stripped, content preserved.
    assert "**" not in body_sent
    assert "`" not in body_sent
    assert "[" not in body_sent and "]" not in body_sent
    assert "bold" in body_sent
    assert "italic" in body_sent
    assert "code" in body_sent
    assert "text" in body_sent
