"""Tests for the Signal channel adapter (G.27 / Tier 4.x).

signal-cli JSON-RPC outbound — text + reactions. Mocks via
``httpx.MockTransport``.
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
        "signal_adapter_test_g27",
        Path(__file__).resolve().parent.parent / "extensions" / "signal" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SignalAdapter, mod


@pytest.fixture
def adapter_with_mock():
    SignalAdapter, _ = _load()
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        # signal-cli always returns a JSON-RPC envelope with a timestamp.
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "send",
                "result": {"timestamp": 1714000000000},
            },
        )

    a = SignalAdapter(
        config={
            "signal_cli_url": "http://localhost:8080",
            "phone_number": "+15551234567",
        }
    )
    a._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Content-Type": "application/json"},
    )
    return a, requests


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_advertises_reactions_only(self) -> None:
        SignalAdapter, _ = _load()
        c = SignalAdapter.capabilities
        assert c & ChannelCapabilities.REACTIONS
        # Edit/delete deferred pending signal-cli version detection.
        assert not (c & ChannelCapabilities.EDIT_MESSAGE)
        assert not (c & ChannelCapabilities.DELETE_MESSAGE)
        assert not (c & ChannelCapabilities.VOICE_OUT)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestSend:
    @pytest.mark.asyncio
    async def test_basic_send(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        result = await adapter.send("+919876543210", "hello signal")
        assert result.success
        assert result.message_id == "1714000000000"
        assert len(requests) == 1
        body = json.loads(requests[0].read())
        assert body["jsonrpc"] == "2.0"
        assert body["method"] == "send"
        assert body["params"]["account"] == "+15551234567"
        assert body["params"]["recipient"] == ["+919876543210"]
        assert body["params"]["message"] == "hello signal"

    @pytest.mark.asyncio
    async def test_send_to_group(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        # Group ids in signal-cli have a "group." prefix.
        await adapter.send("group.abcd1234", "hi group")
        body = json.loads(requests[0].read())
        assert body["params"]["recipient"] == ["group.abcd1234"]

    @pytest.mark.asyncio
    async def test_truncates_to_max(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("+15551234567", "x" * 10_000)
        body = json.loads(requests[0].read())
        assert len(body["params"]["message"]) == 4096

    @pytest.mark.asyncio
    async def test_empty_body_rejected(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        result = await adapter.send("+15551234567", "")
        assert not result.success
        assert len(requests) == 0

    @pytest.mark.asyncio
    async def test_signal_cli_error_returned(self) -> None:
        SignalAdapter, _ = _load()

        def fail_handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "send",
                    "error": {
                        "code": -1,
                        "message": "user not registered with signal",
                    },
                },
            )

        a = SignalAdapter(
            config={
                "signal_cli_url": "http://localhost:8080",
                "phone_number": "+15551234567",
            }
        )
        a._client = httpx.AsyncClient(
            transport=httpx.MockTransport(fail_handler),
            headers={"Content-Type": "application/json"},
        )
        result = await a.send("+919999999999", "x")
        assert not result.success
        assert "user not registered" in result.error


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


class TestReactions:
    @pytest.mark.asyncio
    async def test_reaction_payload_shape(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        result = await adapter.send_reaction(
            "+919876543210", "1714000000000", "👍"
        )
        assert result.success
        body = json.loads(requests[0].read())
        assert body["method"] == "sendReaction"
        params = body["params"]
        assert params["account"] == "+15551234567"
        assert params["recipient"] == ["+919876543210"]
        assert params["emoji"] == "👍"
        assert params["targetAuthor"] == "+15551234567"
        # Timestamp must be int — it's the message id.
        assert params["targetTimestamp"] == 1714000000000
        assert isinstance(params["targetTimestamp"], int)

    @pytest.mark.asyncio
    async def test_empty_emoji_rejected(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        result = await adapter.send_reaction(
            "+919876543210", "1714000000000", ""
        )
        assert not result.success
        assert len(requests) == 0

    @pytest.mark.asyncio
    async def test_non_numeric_message_id_rejected(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        # message_id MUST be a numeric timestamp (signal-cli's identity
        # for messages). Surface a clear error rather than passing junk.
        result = await adapter.send_reaction(
            "+919876543210", "not-a-timestamp", "👍"
        )
        assert not result.success
        assert "timestamp" in result.error
        assert len(requests) == 0
