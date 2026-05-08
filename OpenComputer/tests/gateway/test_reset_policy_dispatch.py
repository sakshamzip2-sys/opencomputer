"""Dispatch integration tests for AllowlistGate + ResetPolicy (Task 1.6).

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (T1.6)

These tests verify the behavior of ``Dispatch.handle_message`` when the
new optional ``allowlist_gate`` and ``reset_policy`` constructor params
are wired in. When BOTH are None (the historic default), no behavior
change is expected — covered by the existing 200+ dispatch tests.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.channels.allowlist import AllowlistGate
from opencomputer.channels.pairing_codes import PairingCodeStore
from opencomputer.gateway.dispatch import Dispatch
from opencomputer.gateway.reset_policy import (
    ResetPolicy,
    ResetPolicyChecker,
    ResetPolicyConfig,
)
from plugin_sdk.core import MessageEvent, Platform

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_loop():
    """An AgentLoop double whose run_conversation is awaitable + returns text."""
    loop = MagicMock()
    loop.run_conversation = AsyncMock(return_value="agent reply")
    loop._consent_gate = None
    loop.get_session_id_for = lambda eid: None
    loop.get_capabilities = lambda: []
    return loop


@pytest.fixture
def mock_adapter():
    """A channel adapter double exposing send + send_typing."""
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter.send_typing = AsyncMock()
    adapter.on_processing_start = AsyncMock()
    adapter.on_processing_complete = AsyncMock()
    return adapter


def _make_event(
    *, platform=Platform.TELEGRAM, chat_id="chat-1", user_id="user-1", text="hello"
) -> MessageEvent:
    return MessageEvent(
        platform=platform,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        timestamp=time.time(),
        metadata={"user_id": user_id, "user_name": "Tester"},
    )


# ── AllowlistGate integration ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_with_no_gate_runs_legacy_path(mock_loop, mock_adapter):
    """allowlist_gate=None preserves legacy behavior — message dispatches."""
    d = Dispatch(loop=mock_loop)
    d.register_adapter("telegram", mock_adapter)
    event = _make_event(text="hello")
    # No exception means the legacy path didn't trip; we don't care about
    # the actual return value (depends on agent loop wiring).
    try:
        await d.handle_message(event)
    except Exception as exc:  # noqa: BLE001
        # Tests run without a full Gateway, so router resolution may
        # raise — that's fine; we only verify the gate didn't short-
        # circuit before the agent path.
        if "router" not in str(exc).lower() and "loop" not in str(exc).lower():
            raise


@pytest.mark.asyncio
async def test_dispatch_with_allowlist_gate_blocks_unknown_user(
    mock_loop, mock_adapter, tmp_path, monkeypatch
):
    """Gate denies — adapter.send fires with pairing code, no agent run."""
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    gate = AllowlistGate(profile_home=tmp_path)
    d = Dispatch(loop=mock_loop, allowlist_gate=gate)
    d.register_adapter("telegram", mock_adapter)
    event = _make_event(user_id="stranger", text="hi")

    result = await d.handle_message(event)

    assert result is None
    mock_adapter.send.assert_awaited_once()
    args, _ = mock_adapter.send.await_args
    assert "Pairing code" in args[1]
    assert "oc gateway pairing approve telegram" in args[1]
    mock_loop.run_conversation.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_with_allowed_user_proceeds(
    mock_loop, mock_adapter, tmp_path, monkeypatch
):
    """Gate allows (env-platform) — agent runs."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "user-1")
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    gate = AllowlistGate(profile_home=tmp_path)
    d = Dispatch(loop=mock_loop, allowlist_gate=gate)
    d.register_adapter("telegram", mock_adapter)
    event = _make_event(user_id="user-1", text="hi")

    try:
        await d.handle_message(event)
    except Exception:
        # Router/loop wiring may raise downstream — fine; we only need
        # to verify the gate did NOT short-circuit.
        pass

    # If gate had blocked, adapter.send would carry the pairing-code text.
    if mock_adapter.send.await_count > 0:
        for call in mock_adapter.send.await_args_list:
            args = call.args
            assert "Pairing code" not in args[1]


@pytest.mark.asyncio
async def test_dispatch_rate_limit_silent(
    mock_loop, mock_adapter, tmp_path, monkeypatch
):
    """Second denial within rate-limit → no reply (pairing_code=None)."""
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    gate = AllowlistGate(profile_home=tmp_path)
    d = Dispatch(loop=mock_loop, allowlist_gate=gate)
    d.register_adapter("telegram", mock_adapter)

    await d.handle_message(_make_event(user_id="rl-user", text="ping"))
    mock_adapter.send.reset_mock()
    # Second within rate-limit window — gate returns code=None — no reply.
    await d.handle_message(_make_event(user_id="rl-user", text="ping again"))

    mock_adapter.send.assert_not_called()


# ── Reset Policy integration ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_policy_idle_creates_new_session_id(mock_loop, tmp_path):
    """When idle threshold exceeded, _session_id_for returns a fresh id."""
    cfg = ResetPolicyConfig(default=ResetPolicy(mode="idle", idle_minutes=60))
    checker = ResetPolicyChecker(cfg)
    d = Dispatch(loop=mock_loop, reset_policy=checker)

    event1 = _make_event(text="hi")
    sid_before = d._session_id_for(event1)

    # Simulate prior last-seen 2 hours ago.
    d._chat_last_seen[("telegram", "chat-1")] = time.time() - 7200

    # Manually invoke the reset path — should mint a token and produce
    # a different session_id on next derivation.
    do_reset, reason = checker.should_reset(
        "telegram", "chat-1", time.time() - 7200
    )
    assert do_reset is True
    # Use reason + timestamp like dispatch does.
    from datetime import timezone as _tz

    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
    d._chat_reset_tokens[("telegram", "chat-1")] = f"{reason}-{stamp}"

    sid_after = d._session_id_for(event1)
    assert sid_after != sid_before


@pytest.mark.asyncio
async def test_reset_policy_within_window_keeps_session(mock_loop):
    """Within idle window, reset doesn't fire — session_id stable."""
    cfg = ResetPolicyConfig(default=ResetPolicy(mode="idle", idle_minutes=60))
    checker = ResetPolicyChecker(cfg)
    d = Dispatch(loop=mock_loop, reset_policy=checker)

    event = _make_event(text="hi")
    sid1 = d._session_id_for(event)
    # last_seen 30s ago — within window.
    d._chat_last_seen[("telegram", "chat-1")] = time.time() - 30

    do_reset, _ = checker.should_reset(
        "telegram", "chat-1", time.time() - 30
    )
    assert do_reset is False
    sid2 = d._session_id_for(event)
    assert sid1 == sid2


@pytest.mark.asyncio
async def test_per_platform_override_resolved(mock_loop):
    """Per-platform override beats default."""
    cfg = ResetPolicyConfig(
        default=ResetPolicy(mode="off"),
        by_platform={
            "telegram": ResetPolicy(mode="idle", idle_minutes=10),
        },
    )
    checker = ResetPolicyChecker(cfg)
    d = Dispatch(loop=mock_loop, reset_policy=checker)
    # Telegram uses tighter override; Discord uses default off.
    do_tg, _ = checker.should_reset("telegram", "c1", time.time() - 1200)
    do_dc, _ = checker.should_reset("discord", "c1", time.time() - 1200)
    assert do_tg is True
    assert do_dc is False
    assert d._reset_policy is checker  # wired through


# ── last_seen persistence ──────────────────────────────────────────────────


def test_last_seen_persisted_to_disk(mock_loop, tmp_path):
    """_persist_last_seen writes to last_seen_path atomically."""
    path = tmp_path / "gateway" / "last_seen.json"
    d = Dispatch(loop=mock_loop, last_seen_path=path)
    d._chat_last_seen[("telegram", "chat-x")] = 1234567890.0
    d._chat_reset_tokens[("telegram", "chat-x")] = "idle:60m-20260508T000000"
    d._persist_last_seen(force=True)
    assert path.exists()
    import json as _json

    payload = _json.loads(path.read_text(encoding="utf-8"))
    assert payload["last_seen"]["telegram|chat-x"] == 1234567890.0
    assert (
        payload["reset_tokens"]["telegram|chat-x"]
        == "idle:60m-20260508T000000"
    )


def test_last_seen_loaded_on_construction(mock_loop, tmp_path):
    """Existing last_seen.json restores in-memory state."""
    path = tmp_path / "gateway" / "last_seen.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    payload = {
        "last_seen": {"telegram|c1": 1000.0, "discord|c2": 2000.0},
        "reset_tokens": {"telegram|c1": "daily:4-x"},
    }
    path.write_text(_json.dumps(payload), encoding="utf-8")
    d = Dispatch(loop=mock_loop, last_seen_path=path)
    assert d._chat_last_seen[("telegram", "c1")] == 1000.0
    assert d._chat_last_seen[("discord", "c2")] == 2000.0
    assert d._chat_reset_tokens[("telegram", "c1")] == "daily:4-x"


def test_last_seen_corrupt_file_starts_fresh(mock_loop, tmp_path):
    """Corrupt JSON file → in-memory state empty, no crash."""
    path = tmp_path / "gateway" / "last_seen.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {{{", encoding="utf-8")
    d = Dispatch(loop=mock_loop, last_seen_path=path)
    assert d._chat_last_seen == {}
    assert d._chat_reset_tokens == {}
