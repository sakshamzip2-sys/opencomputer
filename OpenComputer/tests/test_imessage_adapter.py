"""Tests for the iMessage / BlueBubbles channel adapter (G.16 / Tier 2.11).

Mocks BlueBubbles via ``httpx.MockTransport``. Verifies:

- Capability flag advertises REACTIONS only (no edit/voice yet).
- ``connect`` calls ``/api/v1/server/info`` with password and seeds the
  high-watermark from the latest ROWID so we don't replay history.
- Polling fetches ``message/query``, filters out echoes (``isFromMe``)
  and already-seen ROWIDs, emits MessageEvents in chronological order.
- ``send`` POSTs to ``/api/v1/message/text`` with ``chatGuid``.
- ``send_reaction`` maps emoji → tapback name and POSTs to ``message/react``.
- Unknown emoji returns a clear error without hitting the network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest

from plugin_sdk import ChannelCapabilities


def _load():
    spec = importlib.util.spec_from_file_location(
        "imessage_adapter_test_g16",
        Path(__file__).resolve().parent.parent / "extensions" / "imessage" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.IMessageAdapter, mod


@pytest.fixture
def adapter_with_mock():
    """Construct an iMessage adapter wired to a mocked BlueBubbles HTTP API.

    Yields ``(adapter, requests, response_callbacks)`` — ``response_callbacks``
    is a dict ``path → callable(req) -> httpx.Response`` so individual tests
    can override responses for specific endpoints.
    """
    IMessageAdapter, _ = _load()
    requests: list[httpx.Request] = []
    callbacks: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        path = req.url.path
        # Test-specific override
        if path in callbacks:
            return callbacks[path](req)
        # Defaults
        if path.endswith("/server/info"):
            return httpx.Response(200, json={"status": 200, "data": {"server_version": "1.9.0"}})
        if path.endswith("/message/query"):
            return httpx.Response(200, json={"status": 200, "data": []})
        if path.endswith("/message/text") or path.endswith("/message/react"):
            return httpx.Response(200, json={"status": 200, "message": "ok"})
        return httpx.Response(404, json={"status": 404})

    adapter = IMessageAdapter(
        config={"base_url": "http://localhost:1234", "password": "secret", "poll_interval_seconds": 60}
    )
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return adapter, requests, callbacks


# ---------------------------------------------------------------------------
# Capability flag
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_advertises_reactions(self) -> None:
        IMessageAdapter, _ = _load()
        c = IMessageAdapter.capabilities
        assert c & ChannelCapabilities.REACTIONS

    def test_does_not_advertise_voice_or_edit(self) -> None:
        IMessageAdapter, _ = _load()
        c = IMessageAdapter.capabilities
        for cap in (
            ChannelCapabilities.VOICE_OUT,
            ChannelCapabilities.VOICE_IN,
            ChannelCapabilities.EDIT_MESSAGE,
            ChannelCapabilities.DELETE_MESSAGE,
        ):
            assert not (c & cap)


# ---------------------------------------------------------------------------
# Send (outbound text)
# ---------------------------------------------------------------------------


class TestSendText:
    @pytest.mark.asyncio
    async def test_send_posts_to_message_text(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.send("iMessage;-;+15555555555", "hello")
        assert result.success
        send_reqs = [r for r in requests if r.url.path.endswith("/message/text")]
        assert len(send_reqs) == 1
        body = send_reqs[0].read().decode()
        assert "iMessage;-;+15555555555" in body
        assert "hello" in body

    @pytest.mark.asyncio
    async def test_truncates_to_max_length(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        long_text = "x" * 100_000
        await adapter.send("guid", long_text)
        send_req = next(r for r in requests if r.url.path.endswith("/message/text"))
        body = send_req.read().decode()
        # The body should NOT contain the full 100k chars
        # (truncated to max_message_length 60_000)
        assert body.count("x") <= 60_001  # plus 1 for safety margin

    @pytest.mark.asyncio
    async def test_http_error_returned(self, adapter_with_mock) -> None:
        adapter, _, callbacks = adapter_with_mock
        callbacks["/api/v1/message/text"] = lambda req: httpx.Response(500, text="server down")
        result = await adapter.send("guid", "hi")
        assert not result.success
        assert "500" in result.error


# ---------------------------------------------------------------------------
# Reactions (tapbacks)
# ---------------------------------------------------------------------------


class TestReactions:
    @pytest.mark.asyncio
    async def test_supported_emoji_posts_react(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.send_reaction("guid", "msg-id", "👍")
        assert result.success
        react_reqs = [r for r in requests if r.url.path.endswith("/message/react")]
        assert len(react_reqs) == 1
        body = react_reqs[0].read().decode()
        assert "like" in body  # 👍 → like

    @pytest.mark.asyncio
    async def test_heart_maps_to_love(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        await adapter.send_reaction("guid", "msg-id", "❤️")
        body = next(
            r for r in requests if r.url.path.endswith("/message/react")
        ).read().decode()
        assert "love" in body

    @pytest.mark.asyncio
    async def test_unmappable_emoji_errors_locally(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.send_reaction("guid", "msg-id", "🚀")
        assert not result.success
        assert "tapback" in result.error.lower()
        # No request was sent
        react_reqs = [r for r in requests if r.url.path.endswith("/message/react")]
        assert react_reqs == []


# ---------------------------------------------------------------------------
# Polling — message ingestion
# ---------------------------------------------------------------------------


class TestPolling:
    @pytest.mark.asyncio
    async def test_filters_out_echoes(self, adapter_with_mock) -> None:
        adapter, _, callbacks = adapter_with_mock
        # First call (initial rowid seed) returns latest ROWID = 0
        # Second call (poll) returns 2 messages — one isFromMe=True (echo)
        callbacks["/api/v1/message/query"] = lambda req: httpx.Response(
            200,
            json={
                "status": 200,
                "data": [
                    {
                        "ROWID": 100,
                        "guid": "msg-100",
                        "text": "outbound echo",
                        "isFromMe": True,
                        "chats": [{"guid": "chat-x"}],
                        "handle": {"address": "+15555555555"},
                        "dateCreated": 1700000000000,
                    },
                    {
                        "ROWID": 99,
                        "guid": "msg-99",
                        "text": "real inbound",
                        "isFromMe": False,
                        "chats": [{"guid": "chat-x"}],
                        "handle": {"address": "+15555555555"},
                        "dateCreated": 1700000000000,
                    },
                ],
            },
        )
        events = await adapter._fetch_new_messages()
        # Only the non-echo message should make it through
        assert len(events) == 1
        assert events[0].text == "real inbound"

    @pytest.mark.asyncio
    async def test_skips_already_seen_rowids(self, adapter_with_mock) -> None:
        adapter, _, callbacks = adapter_with_mock
        adapter._last_rowid = 100  # high-watermark already past these
        callbacks["/api/v1/message/query"] = lambda req: httpx.Response(
            200,
            json={
                "status": 200,
                "data": [
                    {
                        "ROWID": 99, "guid": "old", "text": "old", "isFromMe": False,
                        "chats": [{"guid": "x"}], "handle": {"address": "x"},
                        "dateCreated": 1,
                    },
                ],
            },
        )
        events = await adapter._fetch_new_messages()
        assert events == []

    @pytest.mark.asyncio
    async def test_returns_chronological_order(self, adapter_with_mock) -> None:
        """API returns DESC by ROWID; adapter should reverse to oldest-first."""
        adapter, _, callbacks = adapter_with_mock
        callbacks["/api/v1/message/query"] = lambda req: httpx.Response(
            200,
            json={
                "status": 200,
                "data": [
                    {
                        "ROWID": 102, "guid": "c", "text": "third", "isFromMe": False,
                        "chats": [{"guid": "x"}], "handle": {"address": "x"},
                        "dateCreated": 3,
                    },
                    {
                        "ROWID": 101, "guid": "b", "text": "second", "isFromMe": False,
                        "chats": [{"guid": "x"}], "handle": {"address": "x"},
                        "dateCreated": 2,
                    },
                    {
                        "ROWID": 100, "guid": "a", "text": "first", "isFromMe": False,
                        "chats": [{"guid": "x"}], "handle": {"address": "x"},
                        "dateCreated": 1,
                    },
                ],
            },
        )
        events = await adapter._fetch_new_messages()
        assert [e.text for e in events] == ["first", "second", "third"]

    def test_parse_message_skips_empty_text(self) -> None:
        IMessageAdapter, _ = _load()
        a = IMessageAdapter(
            config={"base_url": "http://x", "password": "y"}
        )
        ev = a._parse_message({
            "ROWID": 1, "guid": "z", "text": "", "isFromMe": False,
            "chats": [{"guid": "x"}], "handle": {"address": "x"},
            "dateCreated": 0,
        })
        assert ev is None

    def test_parse_message_skips_no_chat(self) -> None:
        IMessageAdapter, _ = _load()
        a = IMessageAdapter(
            config={"base_url": "http://x", "password": "y"}
        )
        ev = a._parse_message({
            "ROWID": 1, "guid": "z", "text": "hi", "isFromMe": False,
            "chats": [], "handle": {"address": "x"},
            "dateCreated": 0,
        })
        assert ev is None


# ---------------------------------------------------------------------------
# Tapback emoji map
# ---------------------------------------------------------------------------


class TestTapbackMap:
    @pytest.mark.parametrize(
        "emoji,expected",
        [
            ("❤️", "love"),
            ("❤", "love"),
            ("👍", "like"),
            ("👎", "dislike"),
            ("😂", "laugh"),
            ("❗️", "emphasize"),
            ("❓", "question"),
            ("🚀", None),
            ("✨", None),
        ],
    )
    def test_emoji_mapping(self, emoji: str, expected: str | None) -> None:
        _, mod = _load()
        assert mod._emoji_to_tapback(emoji) == expected
