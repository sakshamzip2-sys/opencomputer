"""Tests for WhatsApp formatting + retry-wrapped sends (PR 3b.4).

WhatsApp accepts only ``*bold*``, ``_italic_``, ``~strike~``, and
``\\`\\`\\`code\\`\\`\\``` — no headers, no links. The adapter applies
``plugin_sdk.format_converters.whatsapp_format`` so generic markdown
renders correctly on the wire.

Mention-gating note: the Cloud API webhook payload doesn't surface a
``mentions[]`` array for outbound businesses, so the Hermes
``_message_mentions_bot`` gate has no analogue in this adapter.
Inbound mention-aware filtering (if needed) belongs in whichever
component receives the webhook (extensions/webhook), not here. We
therefore intentionally don't have a gate-by-mention test.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


def _load():
    spec = importlib.util.spec_from_file_location(
        "whatsapp_adapter_pr3b4_format",
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "whatsapp"
        / "adapter.py",
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
        return httpx.Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "messages": [{"id": "wamid.X"}],
            },
        )

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


def _last_body(requests: list[httpx.Request]) -> str:
    return json.loads(requests[-1].read())["text"]["body"]


# ---------------------------------------------------------------------------
# Markdown → WhatsApp syntax on the wire
# ---------------------------------------------------------------------------


class TestFormatOnSend:
    @pytest.mark.asyncio
    async def test_double_asterisk_to_single(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("+15551234567", "**bold** word")
        assert _last_body(requests) == "*bold* word"

    @pytest.mark.asyncio
    async def test_double_underscore_to_single_asterisk(
        self, adapter_with_mock
    ) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("+15551234567", "__bold__ word")
        assert _last_body(requests) == "*bold* word"

    @pytest.mark.asyncio
    async def test_double_tilde_to_single(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("+15551234567", "~~strike~~")
        assert _last_body(requests) == "~strike~"

    @pytest.mark.asyncio
    async def test_link_flattened_to_label_url(
        self, adapter_with_mock
    ) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("+15551234567", "see [docs](https://example.com)")
        assert _last_body(requests) == "see docs (https://example.com)"

    @pytest.mark.asyncio
    async def test_heading_flattened_to_bold(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("+15551234567", "# Title")
        assert _last_body(requests) == "*Title*"

    @pytest.mark.asyncio
    async def test_code_fence_preserved(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("+15551234567", "```\nhi\n```")
        body = _last_body(requests)
        assert body.startswith("```")
        assert "hi" in body

    @pytest.mark.asyncio
    async def test_inline_code_preserved(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("+15551234567", "use `x` for that")
        body = _last_body(requests)
        assert "`x`" in body

    @pytest.mark.asyncio
    async def test_plain_text_passes_through(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("+15551234567", "just plain")
        assert _last_body(requests) == "just plain"


# ---------------------------------------------------------------------------
# Truncation still applies POST-conversion
# ---------------------------------------------------------------------------


class TestTruncation:
    @pytest.mark.asyncio
    async def test_truncates_to_max_after_format(
        self, adapter_with_mock
    ) -> None:
        adapter, requests = adapter_with_mock
        # 10k chars of plain text — still has to be cut to 4096
        await adapter.send("+15551234567", "x" * 10_000)
        body = _last_body(requests)
        assert len(body) == 4096


# ---------------------------------------------------------------------------
# Retry wrap on transient ConnectError
# ---------------------------------------------------------------------------


class TestRetry:
    @pytest.mark.asyncio
    async def test_transient_connecterror_retried(self) -> None:
        WhatsAppAdapter, _ = _load()
        attempts = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("net down")
            return httpx.Response(
                200, json={"messages": [{"id": "wamid.Y"}]}
            )

        a = WhatsAppAdapter(
            config={"access_token": "x", "phone_number_id": "112233"}
        )
        a._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={
                "Authorization": "Bearer x",
                "Content-Type": "application/json",
            },
        )
        result = await a.send("+15551234567", "hi")
        assert result.success
        assert attempts["n"] == 2

    @pytest.mark.asyncio
    async def test_400_not_retried(self) -> None:
        """Cloud-API 400 isn't transient; should fail fast (no retry)."""
        WhatsAppAdapter, _ = _load()
        attempts = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(
                400, json={"error": {"message": "bad recipient"}}
            )

        a = WhatsAppAdapter(
            config={"access_token": "x", "phone_number_id": "112233"}
        )
        a._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={
                "Authorization": "Bearer x",
                "Content-Type": "application/json",
            },
        )
        result = await a.send("+15551234567", "hi")
        assert not result.success
        # No retry — single attempt only.
        assert attempts["n"] == 1
