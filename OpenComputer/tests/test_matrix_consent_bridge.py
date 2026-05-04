"""Tests for the Matrix → ConsentGate bridge (Wave 6.E.4)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from extensions.matrix.approval import ApprovalQueue
from extensions.matrix.consent_bridge import (
    DEFAULT_TIMEOUT_SECONDS,
    make_matrix_prompt_handler,
    parse_consent_config,
    register_matrix_consent_handler,
)

# ---- parse_consent_config ----


def test_config_defaults_for_none():
    enabled, chat_id, timeout = parse_consent_config(None)
    assert enabled is False
    assert chat_id == ""
    assert timeout == DEFAULT_TIMEOUT_SECONDS


def test_config_defaults_for_empty_dict():
    enabled, chat_id, timeout = parse_consent_config({})
    assert enabled is False
    assert chat_id == ""


def test_config_enabled_with_chat():
    enabled, chat_id, timeout = parse_consent_config({
        "consent_handler": True,
        "consent_chat_id": "!room:server",
        "consent_timeout_seconds": 60,
    })
    assert enabled is True
    assert chat_id == "!room:server"
    assert timeout == 60.0


def test_config_rejects_wrong_types():
    enabled, chat_id, timeout = parse_consent_config({
        "consent_handler": "yes",   # not bool
        "consent_chat_id": 123,     # not str
        "consent_timeout_seconds": -1,  # not positive
    })
    assert enabled is False
    assert chat_id == ""
    assert timeout == DEFAULT_TIMEOUT_SECONDS


# ---- handler ----


def _fake_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.approval_queue = ApprovalQueue()
    adapter._inbound_enabled = True
    fake_send_result = MagicMock(spec=["platform_message_id"])
    fake_send_result.platform_message_id = "$evt-test"
    adapter.send = AsyncMock(return_value=fake_send_result)
    return adapter


def _fake_claim(cap_id: str = "Bash.execute") -> MagicMock:
    claim = MagicMock()
    claim.capability_id = cap_id
    return claim


@pytest.mark.asyncio
async def test_handler_returns_false_when_no_approval_queue():
    """Adapter without an approval_queue → handler returns False so gate auto-denies."""
    gate = MagicMock()
    adapter = MagicMock()
    adapter.approval_queue = None

    handler = make_matrix_prompt_handler(
        gate=gate, adapter=adapter, chat_id="!r:s",
    )
    out = await handler("session-1", _fake_claim(), None)
    assert out is False


@pytest.mark.asyncio
async def test_handler_returns_false_when_inbound_sync_off():
    """The bridge needs /sync running to ever resolve. Without it, deny up-front."""
    gate = MagicMock()
    adapter = _fake_adapter()
    adapter._inbound_enabled = False

    handler = make_matrix_prompt_handler(
        gate=gate, adapter=adapter, chat_id="!r:s",
    )
    out = await handler("session-1", _fake_claim(), None)
    assert out is False


@pytest.mark.asyncio
async def test_handler_returns_true_after_dispatch():
    """When matrix is configured, prompt dispatches + handler returns True."""
    gate = MagicMock()
    gate.resolve_pending = MagicMock(return_value=True)
    adapter = _fake_adapter()

    handler = make_matrix_prompt_handler(
        gate=gate, adapter=adapter, chat_id="!r:s", timeout=2.0,
    )
    out = await handler("session-1", _fake_claim(), None)
    assert out is True
    # adapter.send fires inside the watcher task — wait one tick.
    await asyncio.sleep(0.05)
    adapter.send.assert_called_once()
    args, _ = adapter.send.call_args
    assert args[0] == "!r:s"
    assert "Bash.execute" in args[1]


@pytest.mark.asyncio
async def test_resolve_pending_called_on_allow_reaction():
    """User reacts ✅ → resolve_pending(decision=True) eventually fires."""
    gate = MagicMock()
    gate.resolve_pending = MagicMock(return_value=True)
    adapter = _fake_adapter()

    handler = make_matrix_prompt_handler(
        gate=gate, adapter=adapter, chat_id="!r:s", timeout=2.0,
    )
    await handler("sess-A", _fake_claim("X.read"), "/path/to/file")

    # Simulate a reaction landing — fires the future directly.
    await asyncio.sleep(0.05)
    adapter.approval_queue.on_reaction("$evt-test", "✅")
    # Give the watcher task a chance to run
    await asyncio.sleep(0.1)

    gate.resolve_pending.assert_called_once()
    kwargs = gate.resolve_pending.call_args.kwargs
    assert kwargs["session_id"] == "sess-A"
    assert kwargs["capability_id"] == "X.read"
    assert kwargs["decision"] is True
    assert kwargs["persist"] is False


@pytest.mark.asyncio
async def test_resolve_pending_called_on_deny_reaction():
    gate = MagicMock()
    gate.resolve_pending = MagicMock(return_value=True)
    adapter = _fake_adapter()
    handler = make_matrix_prompt_handler(
        gate=gate, adapter=adapter, chat_id="!r:s", timeout=2.0,
    )
    await handler("sess-B", _fake_claim("X.write"), None)
    await asyncio.sleep(0.05)
    adapter.approval_queue.on_reaction("$evt-test", "❌")
    await asyncio.sleep(0.1)
    kwargs = gate.resolve_pending.call_args.kwargs
    assert kwargs["decision"] is False


@pytest.mark.asyncio
async def test_resolve_pending_timeout_resolves_false():
    gate = MagicMock()
    gate.resolve_pending = MagicMock(return_value=True)
    adapter = _fake_adapter()
    handler = make_matrix_prompt_handler(
        gate=gate, adapter=adapter, chat_id="!r:s", timeout=0.05,
    )
    await handler("sess-C", _fake_claim(), None)
    # request_approval awaits the future with timeout+1.0s → ~1.05s.
    # Wait long enough for the wait_for timeout to elapse.
    await asyncio.sleep(1.3)
    kwargs = gate.resolve_pending.call_args.kwargs
    assert kwargs["decision"] is False


@pytest.mark.asyncio
async def test_register_with_empty_chat_id_is_noop():
    gate = MagicMock()
    adapter = _fake_adapter()
    register_matrix_consent_handler(gate=gate, adapter=adapter, chat_id="")
    # set_prompt_handler should NOT have been called
    gate.set_prompt_handler.assert_not_called()


@pytest.mark.asyncio
async def test_register_installs_handler():
    gate = MagicMock()
    adapter = _fake_adapter()
    register_matrix_consent_handler(
        gate=gate, adapter=adapter, chat_id="!r:s", timeout=10.0,
    )
    gate.set_prompt_handler.assert_called_once()
    handler = gate.set_prompt_handler.call_args.args[0]
    assert callable(handler)
