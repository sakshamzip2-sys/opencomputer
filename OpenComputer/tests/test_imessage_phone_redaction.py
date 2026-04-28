"""iMessage adapter phone-redaction (PR 3c.4).

The BlueBubbles bridge identifies chats by GUIDs that often embed
E.164 phone numbers (``iMessage;-;+15551234567``). The handle on
inbound messages also commonly is a raw phone. PR 3c.4 routes any log
line that interpolates a chat GUID or sender handle through the
helpers added to ``extensions/imessage/adapter.py`` so personal
contacts don't leak into log files.

Covers four log paths:
- inbound message log line (info)
- send error: HTTP non-200
- send error: bluebubbles error envelope
- network exception during send
- reaction error path
- pure unit tests for ``_redact_chat_guid`` and ``_redact_handle``
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest


def _load():
    spec = importlib.util.spec_from_file_location(
        "imessage_adapter_redaction_test",
        Path(__file__).resolve().parent.parent / "extensions" / "imessage" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PHONE = "+15551234567"
_CHAT_GUID = f"iMessage;-;{_PHONE}"


@pytest.fixture
def adapter():
    mod = _load()
    requests: list[httpx.Request] = []
    cb: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        path = req.url.path
        if path in cb:
            return cb[path](req)
        if path.endswith("/server/info"):
            return httpx.Response(200, json={"status": 200, "data": {"server_version": "1"}})
        if path.endswith("/message/text") or path.endswith("/message/react"):
            return httpx.Response(200, json={"status": 200})
        return httpx.Response(404, json={})

    a = mod.IMessageAdapter(
        config={"base_url": "http://localhost:1234", "password": "x", "poll_interval_seconds": 60}
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a, mod, requests, cb


def _assert_redacted(records: list[logging.LogRecord], phone: str) -> None:
    joined = "\n".join(r.getMessage() for r in records)
    assert phone not in joined, f"raw phone {phone} leaked:\n{joined}"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_redact_chat_guid_one_to_one(self, adapter) -> None:
        _, mod, _, _ = adapter
        out = mod._redact_chat_guid("iMessage;-;+15551234567")
        assert "+15551234567" not in out
        assert "+1***4567" in out

    def test_redact_chat_guid_sms_bridge(self, adapter) -> None:
        _, mod, _, _ = adapter
        out = mod._redact_chat_guid("SMS;-;+15551234567")
        assert "+15551234567" not in out
        assert "+1***4567" in out
        assert out.startswith("SMS;-;")

    def test_redact_chat_guid_group_passthrough(self, adapter) -> None:
        _, mod, _, _ = adapter
        # No phone embedded → unchanged.
        guid = "iMessage;+;chat0123456789abcdef"
        assert mod._redact_chat_guid(guid) == guid

    def test_redact_chat_guid_empty(self, adapter) -> None:
        _, mod, _, _ = adapter
        assert mod._redact_chat_guid("") == ""

    def test_redact_handle_phone(self, adapter) -> None:
        _, mod, _, _ = adapter
        assert mod._redact_handle("+15551234567") == "+1***4567"

    def test_redact_handle_email_passes(self, adapter) -> None:
        _, mod, _, _ = adapter
        # Email handles are already used as user IDs by the rest of the
        # system. Don't garble them.
        assert mod._redact_handle("alice@icloud.com") == "alice@icloud.com"


# ---------------------------------------------------------------------------
# Inbound parse log line
# ---------------------------------------------------------------------------


class TestInboundParseLog:
    def test_parse_message_logs_redacted(self, adapter, caplog) -> None:
        a, _, _, _ = adapter
        raw = {
            "isFromMe": False,
            "text": "hi there",
            "chats": [{"guid": _CHAT_GUID}],
            "handle": {"address": _PHONE},
            "dateCreated": 1714000000000,
            "guid": "msg-1",
            "ROWID": 1,
        }
        with caplog.at_level(logging.INFO, logger="opencomputer.ext.imessage"):
            ev = a._parse_message(raw)
        assert ev is not None
        _assert_redacted(caplog.records, _PHONE)


# ---------------------------------------------------------------------------
# Send error paths
# ---------------------------------------------------------------------------


class TestSendErrorRedaction:
    @pytest.mark.asyncio
    async def test_send_http_500_redacts(self, adapter, caplog) -> None:
        a, _, _, cb = adapter
        cb["/api/v1/message/text"] = lambda r: httpx.Response(500, text="boom")
        with caplog.at_level(logging.WARNING, logger="opencomputer.ext.imessage"):
            res = await a.send(_CHAT_GUID, "hi")
        assert not res.success
        _assert_redacted(caplog.records, _PHONE)

    @pytest.mark.asyncio
    async def test_send_bluebubbles_error_redacts(self, adapter, caplog) -> None:
        a, _, _, cb = adapter
        cb["/api/v1/message/text"] = lambda r: httpx.Response(
            200, json={"status": "error", "message": "no chat"}
        )
        with caplog.at_level(logging.WARNING, logger="opencomputer.ext.imessage"):
            res = await a.send(_CHAT_GUID, "hi")
        assert not res.success
        _assert_redacted(caplog.records, _PHONE)

    @pytest.mark.asyncio
    async def test_send_network_exception_redacts(self, adapter, caplog) -> None:
        a, _, _, cb = adapter

        def boom(req):
            raise httpx.ConnectError("connect failed")

        cb["/api/v1/message/text"] = boom
        with caplog.at_level(logging.ERROR, logger="opencomputer.ext.imessage"):
            res = await a.send(_CHAT_GUID, "hi")
        assert not res.success
        _assert_redacted(caplog.records, _PHONE)


# ---------------------------------------------------------------------------
# Reaction error path
# ---------------------------------------------------------------------------


class TestReactionErrorRedaction:
    @pytest.mark.asyncio
    async def test_react_http_404_redacts(self, adapter, caplog) -> None:
        a, _, _, cb = adapter
        cb["/api/v1/message/react"] = lambda r: httpx.Response(404, text="no")
        with caplog.at_level(logging.WARNING, logger="opencomputer.ext.imessage"):
            res = await a.send_reaction(_CHAT_GUID, "msg-1", "👍")
        assert not res.success
        _assert_redacted(caplog.records, _PHONE)
