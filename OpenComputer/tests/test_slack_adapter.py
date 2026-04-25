"""Tests for the Slack channel adapter (G.17 / Tier 2.12).

Web API only — outbound + reactions + edit + delete. Inbound is via
the webhook adapter; not tested here.

Mocks Slack Web API via ``httpx.MockTransport``. Verifies request shape
+ error handling for each endpoint.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from plugin_sdk import ChannelCapabilities


def _load():
    spec = importlib.util.spec_from_file_location(
        "slack_adapter_test_g17",
        Path(__file__).resolve().parent.parent / "extensions" / "slack" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SlackAdapter, mod


@pytest.fixture
def adapter_with_mock():
    SlackAdapter, _ = _load()
    requests = []
    overrides: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        path = req.url.path
        if path in overrides:
            return overrides[path](req)
        if path.endswith("/auth.test"):
            return httpx.Response(200, json={"ok": True, "user": "testbot", "team": "ws"})
        if path.endswith("/chat.postMessage"):
            return httpx.Response(200, json={"ok": True, "ts": "1234567890.123456"})
        if path.endswith("/reactions.add"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/chat.update"):
            return httpx.Response(200, json={"ok": True, "ts": "1234567890.123456"})
        if path.endswith("/chat.delete"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False, "error": f"unmocked: {path}"})

    a = SlackAdapter(config={"bot_token": "xoxb-test"})
    a._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer xoxb-test", "Content-Type": "application/json; charset=utf-8"},
    )
    return a, requests, overrides


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_advertises_g17_caps(self) -> None:
        SlackAdapter, _ = _load()
        c = SlackAdapter.capabilities
        for cap in (
            ChannelCapabilities.REACTIONS,
            ChannelCapabilities.EDIT_MESSAGE,
            ChannelCapabilities.DELETE_MESSAGE,
            ChannelCapabilities.THREADS,
        ):
            assert c & cap

    def test_does_not_advertise_voice_or_typing(self) -> None:
        SlackAdapter, _ = _load()
        c = SlackAdapter.capabilities
        for cap in (
            ChannelCapabilities.TYPING,
            ChannelCapabilities.VOICE_OUT,
            ChannelCapabilities.PHOTO_OUT,
            ChannelCapabilities.DOCUMENT_OUT,
        ):
            assert not (c & cap)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestSend:
    @pytest.mark.asyncio
    async def test_basic_send(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.send("C1234", "hello")
        assert result.success
        assert result.message_id == "1234567890.123456"
        post_req = next(r for r in requests if r.url.path.endswith("/chat.postMessage"))
        body = json.loads(post_req.read())
        assert body["channel"] == "C1234"
        assert body["text"] == "hello"

    @pytest.mark.asyncio
    async def test_thread_reply(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        await adapter.send("C1234", "in thread", thread_ts="1234567890.000001")
        post_req = next(r for r in requests if r.url.path.endswith("/chat.postMessage"))
        body = json.loads(post_req.read())
        assert body["thread_ts"] == "1234567890.000001"
        assert "reply_broadcast" not in body

    @pytest.mark.asyncio
    async def test_broadcast_reply(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        await adapter.send("C1234", "ack", thread_ts="1.0", broadcast=True)
        body = json.loads(next(
            r for r in requests if r.url.path.endswith("/chat.postMessage")
        ).read())
        assert body["reply_broadcast"] is True

    @pytest.mark.asyncio
    async def test_slack_error_returned(self, adapter_with_mock) -> None:
        adapter, _, overrides = adapter_with_mock
        overrides["/api/chat.postMessage"] = lambda req: httpx.Response(
            200, json={"ok": False, "error": "channel_not_found"}
        )
        result = await adapter.send("C1234", "hi")
        assert not result.success
        assert "channel_not_found" in result.error


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


class TestReactions:
    @pytest.mark.asyncio
    async def test_unicode_emoji_mapped(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        await adapter.send_reaction("C1234", "1234.5678", "👍")
        body = json.loads(next(
            r for r in requests if r.url.path.endswith("/reactions.add")
        ).read())
        assert body["name"] == "thumbsup"
        assert body["timestamp"] == "1234.5678"

    @pytest.mark.asyncio
    async def test_bare_name_passed_through(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        await adapter.send_reaction("C1234", "1234.5678", "fire")
        body = json.loads(next(
            r for r in requests if r.url.path.endswith("/reactions.add")
        ).read())
        assert body["name"] == "fire"

    @pytest.mark.asyncio
    async def test_already_reacted_treated_as_success(self, adapter_with_mock) -> None:
        adapter, _, overrides = adapter_with_mock
        overrides["/api/reactions.add"] = lambda req: httpx.Response(
            200, json={"ok": False, "error": "already_reacted"}
        )
        result = await adapter.send_reaction("C1", "ts", "👍")
        # Idempotent — already_reacted shouldn't be an error
        assert result.success


# ---------------------------------------------------------------------------
# Edit / Delete
# ---------------------------------------------------------------------------


class TestEditDelete:
    @pytest.mark.asyncio
    async def test_edit_calls_chat_update(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.edit_message("C1234", "1234.5678", "updated")
        assert result.success
        body = json.loads(next(
            r for r in requests if r.url.path.endswith("/chat.update")
        ).read())
        assert body["channel"] == "C1234"
        assert body["ts"] == "1234.5678"
        assert body["text"] == "updated"

    @pytest.mark.asyncio
    async def test_delete_calls_chat_delete(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.delete_message("C1234", "1234.5678")
        assert result.success
        body = json.loads(next(
            r for r in requests if r.url.path.endswith("/chat.delete")
        ).read())
        assert body == {"channel": "C1234", "ts": "1234.5678"}


# ---------------------------------------------------------------------------
# Emoji map
# ---------------------------------------------------------------------------


class TestEmojiNameMap:
    @pytest.mark.parametrize(
        "input_,expected",
        [
            ("👍", "thumbsup"),
            ("👎", "thumbsdown"),
            ("❤️", "heart"),
            ("❤", "heart"),
            ("🔥", "fire"),
            ("✅", "white_check_mark"),
            # Bare name pass-through
            ("custom_emoji", "custom_emoji"),
            ("MIXED_Case", "mixed_case"),  # lowercased
            ("", ""),
        ],
    )
    def test_mapping(self, input_: str, expected: str) -> None:
        _, mod = _load()
        assert mod._emoji_to_slack_name(input_) == expected


class TestConnectAuthCheck:
    @pytest.mark.asyncio
    async def test_invalid_token_returns_false(self, adapter_with_mock) -> None:
        adapter, _, overrides = adapter_with_mock
        overrides["/api/auth.test"] = lambda req: httpx.Response(
            200, json={"ok": False, "error": "invalid_auth"}
        )
        ok = await adapter.connect()
        assert ok is False
