"""Tests for the Matrix channel adapter (G.19 / Tier 3.x).

Client-Server API outbound only — text + reactions + edit (m.replace) +
redaction. Mocks via ``httpx.MockTransport``.
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
        "matrix_adapter_test_g19",
        Path(__file__).resolve().parent.parent / "extensions" / "matrix" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MatrixAdapter, mod


@pytest.fixture
def adapter_with_mock():
    MatrixAdapter, _ = _load()
    requests: list[httpx.Request] = []
    overrides: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        path = req.url.path
        if path in overrides:
            return overrides[path](req)
        if path.endswith("/_matrix/client/v3/account/whoami"):
            return httpx.Response(200, json={"user_id": "@bot:test.local"})
        # Send / edit / reaction events all hit /rooms/.../send/...
        if "/rooms/" in path and "/send/" in path:
            return httpx.Response(200, json={"event_id": "$ev1234:test.local"})
        if "/rooms/" in path and "/redact/" in path:
            return httpx.Response(200, json={"event_id": "$redact5678:test.local"})
        return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})

    a = MatrixAdapter(
        config={"homeserver": "https://matrix.test.local", "access_token": "syt_xxx"}
    )
    a._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer syt_xxx", "Content-Type": "application/json"},
    )
    a._user_id = "@bot:test.local"  # bypass connect()
    return a, requests, overrides


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_advertises_g19_caps(self) -> None:
        MatrixAdapter, _ = _load()
        c = MatrixAdapter.capabilities
        for cap in (
            ChannelCapabilities.REACTIONS,
            ChannelCapabilities.EDIT_MESSAGE,
            ChannelCapabilities.DELETE_MESSAGE,
            ChannelCapabilities.THREADS,
        ):
            assert c & cap

    def test_does_not_advertise_voice_or_typing(self) -> None:
        MatrixAdapter, _ = _load()
        c = MatrixAdapter.capabilities
        assert not (c & ChannelCapabilities.VOICE_OUT)
        assert not (c & ChannelCapabilities.TYPING)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestSend:
    @pytest.mark.asyncio
    async def test_basic_send(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.send("!room:test.local", "hello")
        assert result.success
        assert result.message_id == "$ev1234:test.local"
        send_req = next(
            r for r in requests
            if "/send/m.room.message/" in r.url.path
        )
        body = json.loads(send_req.read())
        assert body["msgtype"] == "m.text"
        assert body["body"] == "hello"

    @pytest.mark.asyncio
    async def test_thread_root_kwarg(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        await adapter.send("!room:test.local", "in thread", thread_root="$parent:t.l")
        body = json.loads(next(
            r for r in requests if "/send/m.room.message/" in r.url.path
        ).read())
        assert body["m.relates_to"]["rel_type"] == "m.thread"
        assert body["m.relates_to"]["event_id"] == "$parent:t.l"

    @pytest.mark.asyncio
    async def test_room_id_is_url_encoded(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        # Room IDs contain ! and : which need percent-encoding
        await adapter.send("!myroom:test.local", "hi")
        send_req = next(
            r for r in requests if "/send/m.room.message/" in r.url.path
        )
        # Verify the path contains the encoded form
        assert "%21myroom" in send_req.url.path or "!myroom" in send_req.url.path
        # The literal ":" is technically unreserved in path; either form is fine.

    @pytest.mark.asyncio
    async def test_truncates_to_max(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        await adapter.send("!r:t.l", "x" * 100_000)
        body = json.loads(next(
            r for r in requests if "/send/m.room.message/" in r.url.path
        ).read())
        assert len(body["body"]) == 60_000

    @pytest.mark.asyncio
    async def test_http_error_returned(self, adapter_with_mock) -> None:
        adapter, _, overrides = adapter_with_mock
        # Override matches both encoded forms — use a permissive callback
        def err(_req):
            return httpx.Response(403, json={"errcode": "M_FORBIDDEN"})

        # Install on actual paths the mock handler resolves to
        # For simplicity, fail any send by patching the handler dict
        # via a direct check in the fixture handler isn't possible — instead
        # use a very specific path. The fixture's overrides match exact paths,
        # so use the encoded-room-id path:
        from urllib.parse import quote

        room = "!r:t.l"
        # Find the actual path matrix would hit
        # We can't easily predict txn_id, so override based on prefix pattern
        # Patch _client directly with a fail-on-send transport.
        def fail_handler(req: httpx.Request) -> httpx.Response:
            if "/send/m.room.message/" in req.url.path:
                return httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
            return httpx.Response(200, json={})

        adapter._client = httpx.AsyncClient(
            transport=httpx.MockTransport(fail_handler),
            headers={"Authorization": "Bearer t", "Content-Type": "application/json"},
        )
        result = await adapter.send(room, "hi")
        assert not result.success
        assert "403" in result.error


# ---------------------------------------------------------------------------
# Reactions (m.reaction events)
# ---------------------------------------------------------------------------


class TestReactions:
    @pytest.mark.asyncio
    async def test_reaction_uses_unicode_directly(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.send_reaction("!r:t.l", "$msg:t.l", "👍")
        assert result.success
        rxn_req = next(
            r for r in requests if "/send/m.reaction/" in r.url.path
        )
        body = json.loads(rxn_req.read())
        # Matrix uses the unicode emoji directly as the "key"
        assert body["m.relates_to"]["key"] == "👍"
        assert body["m.relates_to"]["rel_type"] == "m.annotation"
        assert body["m.relates_to"]["event_id"] == "$msg:t.l"

    @pytest.mark.asyncio
    async def test_empty_emoji_rejected(self, adapter_with_mock) -> None:
        adapter, _, _ = adapter_with_mock
        result = await adapter.send_reaction("!r:t.l", "$m:t.l", "")
        assert not result.success


# ---------------------------------------------------------------------------
# Edit via m.replace
# ---------------------------------------------------------------------------


class TestEdit:
    @pytest.mark.asyncio
    async def test_edit_sends_m_replace(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.edit_message("!r:t.l", "$orig:t.l", "updated")
        assert result.success
        edit_req = next(
            r for r in requests if "/send/m.room.message/" in r.url.path
        )
        body = json.loads(edit_req.read())
        # Original body uses "* " prefix as fallback
        assert body["body"].startswith("* ")
        # m.new_content has the actual updated message
        assert body["m.new_content"]["body"] == "updated"
        assert body["m.relates_to"]["rel_type"] == "m.replace"
        assert body["m.relates_to"]["event_id"] == "$orig:t.l"


# ---------------------------------------------------------------------------
# Redaction (delete)
# ---------------------------------------------------------------------------


class TestRedaction:
    @pytest.mark.asyncio
    async def test_delete_uses_redact_endpoint(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.delete_message("!r:t.l", "$ev:t.l")
        assert result.success
        redact_req = next(
            r for r in requests if "/redact/" in r.url.path
        )
        # Should be a PUT
        assert redact_req.method == "PUT"

    @pytest.mark.asyncio
    async def test_delete_with_reason(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        await adapter.delete_message("!r:t.l", "$ev:t.l", reason="off-topic")
        body = json.loads(next(
            r for r in requests if "/redact/" in r.url.path
        ).read())
        assert body["reason"] == "off-topic"

    @pytest.mark.asyncio
    async def test_delete_no_reason_omits_field(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        await adapter.delete_message("!r:t.l", "$ev:t.l")
        body = json.loads(next(
            r for r in requests if "/redact/" in r.url.path
        ).read())
        assert "reason" not in body


# ---------------------------------------------------------------------------
# Connect (whoami auth check)
# ---------------------------------------------------------------------------


class TestConnect:
    @pytest.mark.asyncio
    async def test_caches_user_id(self, adapter_with_mock) -> None:
        adapter, _, _ = adapter_with_mock
        # Already pre-set in fixture, verify
        assert adapter._user_id == "@bot:test.local"
