"""Tests for the WhatsApp channel adapter (G.26 / Tier 4.x).

Cloud API outbound only — text + reactions. Mocks via
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
        "whatsapp_adapter_test_g26",
        Path(__file__).resolve().parent.parent / "extensions" / "whatsapp" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.WhatsAppAdapter, mod


@pytest.fixture
def adapter_with_mock():
    WhatsAppAdapter, _ = _load()
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if "/messages" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "contacts": [{"input": "919876543210", "wa_id": "919876543210"}],
                    "messages": [{"id": "wamid.HBgN..."}],
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    a = WhatsAppAdapter(
        config={"access_token": "EAAG_test", "phone_number_id": "112233"}
    )
    a._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={
            "Authorization": "Bearer EAAG_test",
            "Content-Type": "application/json",
        },
    )
    return a, requests


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_advertises_reactions_only(self) -> None:
        WhatsAppAdapter, _ = _load()
        c = WhatsAppAdapter.capabilities
        assert c & ChannelCapabilities.REACTIONS
        # Edit/delete intentionally not supported by Cloud API for outbound.
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
        result = await adapter.send("+919876543210", "hello whatsapp")
        assert result.success
        assert result.message_id == "wamid.HBgN..."
        assert len(requests) == 1
        body = json.loads(requests[0].read())
        assert body["messaging_product"] == "whatsapp"
        # Leading + must be stripped per Cloud API expectation.
        assert body["to"] == "919876543210"
        assert body["type"] == "text"
        assert body["text"]["body"] == "hello whatsapp"

    @pytest.mark.asyncio
    async def test_path_targets_phone_number_id(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("+15551234567", "hi")
        assert "/v18.0/112233/messages" in requests[0].url.path

    @pytest.mark.asyncio
    async def test_truncates_to_max(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("+15551234567", "x" * 10_000)
        body = json.loads(requests[0].read())
        assert len(body["text"]["body"]) == 4096

    @pytest.mark.asyncio
    async def test_empty_body_rejected(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        result = await adapter.send("+15551234567", "")
        assert not result.success
        # No request should hit the wire.
        assert len(requests) == 0

    @pytest.mark.asyncio
    async def test_http_error_returned(self) -> None:
        WhatsAppAdapter, _ = _load()

        def fail_handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={
                    "error": {"message": "Invalid OAuth access token", "code": 190}
                },
            )

        a = WhatsAppAdapter(
            config={"access_token": "bad", "phone_number_id": "112233"}
        )
        a._client = httpx.AsyncClient(
            transport=httpx.MockTransport(fail_handler),
            headers={
                "Authorization": "Bearer bad",
                "Content-Type": "application/json",
            },
        )
        result = await a.send("+15551234567", "x")
        assert not result.success
        assert "401" in result.error


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


class TestReactions:
    @pytest.mark.asyncio
    async def test_reaction_payload_shape(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        result = await adapter.send_reaction("+15551234567", "wamid.target", "👍")
        assert result.success
        body = json.loads(requests[0].read())
        assert body["type"] == "reaction"
        assert body["reaction"]["message_id"] == "wamid.target"
        assert body["reaction"]["emoji"] == "👍"

    @pytest.mark.asyncio
    async def test_empty_emoji_rejected(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        # Empty emoji would CLEAR reactions per Cloud API, but the
        # adapter rejects it so callers don't accidentally clear when
        # they meant to add. Explicit "clear" path could be added later.
        result = await adapter.send_reaction("+15551234567", "wamid.target", "")
        assert not result.success
        assert len(requests) == 0
