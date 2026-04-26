"""Telegram inline-approval-button surface — round 2a P-5.

Covers the wire-format the adapter emits, the ``callback_query`` →
``ConsentGate.resolve_pending`` round-trip via ``Dispatch``, and the
double-click dedupe (callback_query.id and request_token level).

Telegram HTTP I/O is mocked at the ``httpx.AsyncClient`` boundary so
tests run without network.
"""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from extensions.telegram.adapter import TelegramAdapter
from opencomputer.agent.consent.audit import AuditLogger
from opencomputer.agent.consent.gate import ConsentGate
from opencomputer.agent.consent.store import ConsentStore
from opencomputer.agent.state import apply_migrations
from opencomputer.gateway.dispatch import Dispatch
from plugin_sdk import CapabilityClaim, ConsentTier
from plugin_sdk.core import SendResult


def _make_adapter() -> TelegramAdapter:
    a = TelegramAdapter({"bot_token": "test-token"})
    # Pre-stub the httpx client so send_approval_request can fire
    # without opening a real connection.
    a._client = AsyncMock()
    return a


def _setup_gate() -> tuple[ConsentGate, ConsentStore, AuditLogger]:
    tmp = Path(tempfile.mkdtemp())
    conn = sqlite3.connect(tmp / "t.db", check_same_thread=False)
    apply_migrations(conn)
    store = ConsentStore(conn)
    audit = AuditLogger(conn, hmac_key=b"k" * 16)
    return ConsentGate(store=store, audit=audit), store, audit


# ─── Wire format ─────────────────────────────────────────────────────


async def test_send_approval_request_emits_three_inline_buttons() -> None:
    """The outbound sendMessage payload carries the ``[once, always, deny]`` row."""
    adapter = _make_adapter()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"ok": True, "result": {"message_id": 42}}
    adapter._client.post = AsyncMock(return_value=fake_resp)

    result = await adapter.send_approval_request(
        chat_id="5555",
        prompt_text="Allow read_files.metadata on /tmp/x? [y/N/always]",
        request_token="abc123",
    )
    assert isinstance(result, SendResult)
    assert result.success is True

    adapter._client.post.assert_awaited_once()
    args, kwargs = adapter._client.post.call_args
    assert args[0].endswith("/sendMessage")
    payload = kwargs["json"]
    assert payload["chat_id"] == "5555"
    assert "Allow read_files.metadata" in payload["text"]
    keyboard = payload["reply_markup"]["inline_keyboard"]
    assert len(keyboard) == 1
    row = keyboard[0]
    assert len(row) == 3
    assert [b["text"] for b in row] == [
        "✓ Allow once", "✓ Allow always", "✗ Deny",
    ]
    assert [b["callback_data"] for b in row] == [
        "oc:approve:once:abc123",
        "oc:approve:always:abc123",
        "oc:approve:deny:abc123",
    ]
    # Token must be remembered so the callback handler can edit later.
    assert "abc123" in adapter._approval_tokens


async def test_send_approval_request_propagates_http_error() -> None:
    adapter = _make_adapter()
    bad_resp = MagicMock()
    bad_resp.status_code = 400
    bad_resp.text = "Bad Request"
    adapter._client.post = AsyncMock(return_value=bad_resp)
    result = await adapter.send_approval_request(
        chat_id="1", prompt_text="?", request_token="t",
    )
    assert result.success is False
    assert "HTTP 400" in (result.error or "")


# ─── Callback handling + dedupe ──────────────────────────────────────


async def test_callback_query_routes_to_registered_callback() -> None:
    """A ``callback_query`` update calls the registered approval callback."""
    adapter = _make_adapter()
    # Pre-register a token so the callback is treated as live.
    adapter._approval_tokens["tok1"] = {"chat_id": "9", "message_id": 17}
    adapter._client.post = AsyncMock(return_value=MagicMock(
        status_code=200, json=lambda: {"ok": True}
    ))

    received: list[tuple[str, str]] = []

    async def cb(verb: str, token: str) -> None:
        received.append((verb, token))

    adapter.set_approval_callback(cb)
    await adapter._handle_update({
        "update_id": 1,
        "callback_query": {
            "id": "cbq-1",
            "data": "oc:approve:always:tok1",
            "message": {"message_id": 17, "chat": {"id": 9}},
            "from": {"id": 4242},
        },
    })
    assert received == [("always", "tok1")]


async def test_callback_query_double_click_dedupes_by_id() -> None:
    """A retry of the same callback_query.id is dropped without re-firing."""
    adapter = _make_adapter()
    adapter._approval_tokens["tok2"] = {"chat_id": "9", "message_id": 17}
    adapter._client.post = AsyncMock(return_value=MagicMock(
        status_code=200, json=lambda: {"ok": True}
    ))

    received: list[tuple[str, str]] = []

    async def cb(verb: str, token: str) -> None:
        received.append((verb, token))

    adapter.set_approval_callback(cb)
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cbq-dup",
            "data": "oc:approve:once:tok2",
            "message": {"message_id": 17, "chat": {"id": 9}},
            "from": {"id": 4242},
        },
    }
    await adapter._handle_update(update)
    await adapter._handle_update(update)
    assert received == [("once", "tok2")]


async def test_callback_query_double_click_dedupes_by_token() -> None:
    """Two distinct callback_query.ids on the same token only fire once."""
    adapter = _make_adapter()
    adapter._approval_tokens["tok3"] = {"chat_id": "9", "message_id": 17}
    adapter._client.post = AsyncMock(return_value=MagicMock(
        status_code=200, json=lambda: {"ok": True}
    ))
    received: list[tuple[str, str]] = []

    async def cb(verb: str, token: str) -> None:
        received.append((verb, token))

    adapter.set_approval_callback(cb)
    await adapter._handle_update({
        "update_id": 1,
        "callback_query": {
            "id": "cbq-A", "data": "oc:approve:once:tok3",
            "message": {"message_id": 17, "chat": {"id": 9}},
            "from": {"id": 1},
        },
    })
    await adapter._handle_update({
        "update_id": 2,
        "callback_query": {
            "id": "cbq-B", "data": "oc:approve:deny:tok3",
            "message": {"message_id": 17, "chat": {"id": 9}},
            "from": {"id": 1},
        },
    })
    assert received == [("once", "tok3")]


async def test_callback_query_unknown_token_ignored() -> None:
    """A click for a token we never sent is dropped quietly."""
    adapter = _make_adapter()
    adapter._client.post = AsyncMock(return_value=MagicMock(
        status_code=200, json=lambda: {"ok": True}
    ))
    received: list[tuple[str, str]] = []

    async def cb(verb: str, token: str) -> None:
        received.append((verb, token))

    adapter.set_approval_callback(cb)
    await adapter._handle_update({
        "update_id": 1,
        "callback_query": {
            "id": "cbq-orphan", "data": "oc:approve:once:does-not-exist",
            "message": {"message_id": 17, "chat": {"id": 9}},
            "from": {"id": 1},
        },
    })
    assert received == []


async def test_callback_query_non_approval_data_ignored() -> None:
    """Inline buttons not minted by the consent flow don't reach the callback."""
    adapter = _make_adapter()
    adapter._client.post = AsyncMock(return_value=MagicMock(
        status_code=200, json=lambda: {"ok": True}
    ))
    received: list[tuple[str, str]] = []

    async def cb(verb: str, token: str) -> None:
        received.append((verb, token))

    adapter.set_approval_callback(cb)
    await adapter._handle_update({
        "update_id": 1,
        "callback_query": {
            "id": "cbq-other", "data": "some-other-plugin:menu:42",
            "message": {"message_id": 17, "chat": {"id": 9}},
            "from": {"id": 1},
        },
    })
    assert received == []


# ─── Dispatch ↔ Gate ↔ Adapter end-to-end ─────────────────────────


class _LoopStub:
    """Minimal AgentLoop stand-in that exposes a ConsentGate."""

    def __init__(self, gate: ConsentGate) -> None:
        self._consent_gate = gate


async def test_dispatch_registers_prompt_handler_and_routes_clicks() -> None:
    """End-to-end: gate.request_approval → adapter buttons → click → resolve."""
    gate, store, _ = _setup_gate()
    loop = _LoopStub(gate)
    dispatch = Dispatch(loop)  # type: ignore[arg-type]

    # Gate now has dispatch as its prompt handler.
    assert gate._prompt_handler is not None  # type: ignore[truthy-bool]

    # Build adapter, register with dispatch, simulate a recent inbound
    # message so the session_id ↔ chat_id binding is in place.
    adapter = _make_adapter()
    sent_payloads: list[dict[str, Any]] = []

    async def _post(url: str, **kwargs: Any) -> Any:
        sent_payloads.append({"url": url, **kwargs})
        return MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 99}},
        )

    adapter._client.post = AsyncMock(side_effect=_post)

    dispatch.register_adapter("telegram", adapter)
    # Manually seed the binding (skips the run_conversation pathway).
    dispatch._session_channels["sess-1"] = (adapter, "55")

    claim = CapabilityClaim(
        capability_id="read_files.metadata",
        tier_required=ConsentTier.PER_ACTION,
        human_description="read",
    )

    # Kick off request_approval; the gate calls dispatch's prompt
    # handler, which calls adapter.send_approval_request, which posts
    # the inline buttons. We then simulate the user clicking "always".
    async def _click_after_send() -> None:
        # Wait until the adapter has minted a token (i.e. the prompt
        # has been sent), then drive the callback.
        for _ in range(50):
            if adapter._approval_tokens:
                break
            await asyncio.sleep(0.01)
        assert adapter._approval_tokens, "adapter never received a prompt"
        token = next(iter(adapter._approval_tokens.keys()))
        await adapter._handle_update({
            "update_id": 1,
            "callback_query": {
                "id": "cbq-X",
                "data": f"oc:approve:always:{token}",
                "message": {"message_id": 99, "chat": {"id": 55}},
                "from": {"id": 1},
            },
        })

    click_task = asyncio.create_task(_click_after_send())
    decision = await gate.request_approval(
        claim=claim, scope="/tmp/foo.py", session_id="sess-1", timeout_s=5.0,
    )
    await click_task

    assert decision.allowed is True
    assert "allow always" in decision.reason
    grant = store.get("read_files.metadata", "/tmp/foo.py")
    assert grant is not None  # allow_always persisted
    # Adapter must have actually dispatched the inline-keyboard message.
    assert any(p.get("url", "").endswith("/sendMessage") for p in sent_payloads)


async def test_dispatch_no_binding_returns_false_from_handler() -> None:
    """Sessions without an adapter binding cause prompt handler to return False."""
    gate, _, _ = _setup_gate()
    loop = _LoopStub(gate)
    dispatch = Dispatch(loop)  # type: ignore[arg-type]
    claim = CapabilityClaim(
        capability_id="x", tier_required=ConsentTier.PER_ACTION,
        human_description="",
    )
    sent = await dispatch._send_approval_prompt("unknown-session", claim, None)
    assert sent is False
