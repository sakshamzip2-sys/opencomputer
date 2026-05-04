"""Tests for ACPSession's /steer + /queue Hermes-port (Wave 5 T3).

The ACP server transports JSON-RPC over stdio; the per-session state
lives on :class:`opencomputer.acp.session.ACPSession`. ``/steer``
interrupts an in-flight turn with new user text; ``/queue`` appends
followups that drain after the current turn finishes.

These tests exercise the in-memory contract directly — we don't spin up
the JSON-RPC stdio loop because the server-side dispatch is a thin
shim that just calls the methods we test here.
"""

from __future__ import annotations

import pytest

from opencomputer.acp.session import ACPSession, QueuedMessage


def _make_session() -> ACPSession:
    """Build an ACPSession with a no-op send callback (we never assert wire I/O here)."""
    return ACPSession(session_id="test-sid", send=lambda _m, _p: None)


@pytest.mark.asyncio
async def test_steer_interrupts_running_turn():
    sess = _make_session()
    sess.mark_running()
    await sess.steer("change direction please")
    assert sess.is_interrupted is True
    assert sess.pending_user_text == "change direction please"


@pytest.mark.asyncio
async def test_steer_idle_session_still_records_intent():
    """A /steer on an idle session is treated like the next user message."""
    sess = _make_session()
    # No mark_running() — session is idle
    await sess.steer("idle steer")
    assert sess.is_interrupted is True
    assert sess.pending_user_text == "idle steer"


@pytest.mark.asyncio
async def test_queue_appends_to_buffer():
    sess = _make_session()
    sess.mark_running()
    await sess.queue("first followup")
    await sess.queue("second followup")
    assert len(sess.queued) == 2
    assert sess.queued[0].text == "first followup"
    assert sess.queued[1].text == "second followup"


@pytest.mark.asyncio
async def test_queue_idle_session_treated_as_normal_message():
    sess = _make_session()
    # No mark_running() → idle; queue still buffers it for next turn entry
    await sess.queue("hello")
    assert len(sess.queued) == 1
    assert sess.queued[0].text == "hello"


@pytest.mark.asyncio
async def test_drain_queue_after_turn_ends():
    sess = _make_session()
    sess.mark_running()
    await sess.queue("a")
    await sess.queue("b")
    sess.mark_idle()
    drained = sess.drain_queue()
    assert [m.text for m in drained] == ["a", "b"]
    assert sess.queued == []


@pytest.mark.asyncio
async def test_drain_clears_interrupt_state_too():
    """Drain after a steer also clears the in-flight interrupt."""
    sess = _make_session()
    sess.mark_running()
    await sess.steer("redirect")
    assert sess.is_interrupted is True
    sess.mark_idle()
    assert sess.is_interrupted is False
    # consume_pending_user_text returns the steer text exactly once
    assert sess.consume_pending_user_text() == "redirect"
    assert sess.consume_pending_user_text() is None


@pytest.mark.asyncio
async def test_queued_message_dataclass():
    qm = QueuedMessage(text="x")
    assert qm.text == "x"
