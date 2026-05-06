"""Tests for collect-mode dispatcher integration (Phase 2 follow-on).

The leader-of-debounce-window pattern: in collect mode, the FIRST arrival
per drain window runs the agent on the merged buffer; subsequent arrivals
buffer + reset the timer + return early. Existing followup/interrupt
behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from opencomputer.gateway.dispatch import Dispatch
from plugin_sdk.core import MessageEvent, Platform
from plugin_sdk.queue import QueueConfig


def _make_loop():
    """MagicMock-based loop stub matching how other dispatch tests build one."""
    calls: list[dict] = []
    fake_loop = MagicMock()

    async def fake_run(user_message: str, session_id: str, **kw):
        calls.append(
            {"text": user_message, "session_id": session_id, **kw}
        )
        result = MagicMock()
        result.final_message = MagicMock(content="ok")
        return result

    fake_loop.run_conversation = fake_run
    fake_loop.calls = calls  # type: ignore[attr-defined]
    return fake_loop


def _evt(text: str, ts: float = 0.0) -> MessageEvent:
    return MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="42",
        user_id="u",
        text=text,
        timestamp=ts,
        attachments=[],
        metadata={},
    )


async def test_collect_mode_buffers_and_merges_two_arrivals(tmp_path, monkeypatch):
    """Two messages within debounce → one run with merged text."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))

    loop = _make_loop()
    dispatch = Dispatch(loop=loop)

    # Tight debounce so the test runs fast.
    qm = dispatch._queue_manager
    qm.set_session_config(
        "irrelevant_will_be_overwritten_below",
        QueueConfig(mode="collect", collect_debounce_s=0.1),
    )
    # Resolve the deterministic session_id Dispatch uses (sha256 of the
    # platform/chat_id pair, first 32 hex chars).
    import hashlib
    session_id = hashlib.sha256(b"telegram:42").hexdigest()[:32]
    qm.set_session_config(
        session_id, QueueConfig(mode="collect", collect_debounce_s=0.1)
    )

    e1 = _evt("hello")
    e2 = _evt("world")

    # Fire both concurrently; the second should be buffered into the same drain.
    t1 = asyncio.create_task(dispatch.handle_message(e1))
    await asyncio.sleep(0.02)
    t2 = asyncio.create_task(dispatch.handle_message(e2))
    await asyncio.gather(t1, t2)

    # The agent should have been called exactly ONCE with merged text.
    assert len(loop.calls) == 1, f"expected 1 call, got {len(loop.calls)}"
    merged = loop.calls[0]["text"]
    assert "hello" in merged
    assert "world" in merged


async def test_followup_mode_unchanged_no_buffering(tmp_path, monkeypatch):
    """Default followup mode: each arrival runs its own agent call."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))

    loop = _make_loop()
    dispatch = Dispatch(loop=loop)

    e1 = _evt("first")
    e2 = _evt("second")
    await dispatch.handle_message(e1)
    await dispatch.handle_message(e2)

    assert len(loop.calls) == 2
    assert loop.calls[0]["text"] == "first"
    assert loop.calls[1]["text"] == "second"


async def test_collect_mode_leader_cleared_after_drain(tmp_path, monkeypatch):
    """After the leader's drain run, a subsequent message starts a fresh window."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))

    loop = _make_loop()
    dispatch = Dispatch(loop=loop)

    import hashlib
    session_id = hashlib.sha256(b"telegram:42").hexdigest()[:32]
    qm = dispatch._queue_manager
    qm.set_session_config(
        session_id, QueueConfig(mode="collect", collect_debounce_s=0.05)
    )

    await dispatch.handle_message(_evt("alpha"))
    # Leader should be cleared; new message starts fresh.
    assert dispatch._collect_leaders.get(session_id) is None

    await dispatch.handle_message(_evt("beta"))
    assert dispatch._collect_leaders.get(session_id) is None

    assert len(loop.calls) == 2
