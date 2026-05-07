"""Tests for steer replan-with-context (PR-A Feature 1)."""

from __future__ import annotations

import asyncio
import threading

import pytest

# ---------------------------------------------------------------------------
# Cancel-event API on SteerRegistry
# ---------------------------------------------------------------------------


def test_cancel_event_lazy_creation():
    """``cancel_event`` returns the same Event instance on repeated calls."""
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    ev = reg.cancel_event("sid-1")
    assert isinstance(ev, asyncio.Event)
    assert reg.cancel_event("sid-1") is ev


def test_submit_sets_cancel_event():
    """Calling submit signals the per-session cancel event."""
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    ev = reg.cancel_event("sid-1")
    assert not ev.is_set()
    reg.submit("sid-1", "go left instead")
    assert ev.is_set()


def test_reset_cancel_clears_event():
    """``reset_cancel`` clears a previously-set event."""
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    reg.submit("sid-1", "first")
    ev = reg.cancel_event("sid-1")
    assert ev.is_set()
    reg.reset_cancel("sid-1")
    assert not ev.is_set()


def test_has_cancel_listener_public_api():
    """``has_cancel_listener`` reports whether a session has an event allocated."""
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    assert reg.has_cancel_listener("sid-untouched") is False
    reg.cancel_event("sid-1")
    assert reg.has_cancel_listener("sid-1") is True


def test_format_nudge_message_interrupted_flag():
    """``was_interrupted=True`` switches the prefix to ``<USER-INTERRUPT>``."""
    from opencomputer.agent.steer import format_nudge_message

    text = format_nudge_message("change direction", was_interrupted=True)
    assert "<USER-INTERRUPT>" in text
    assert "change direction" in text
    text2 = format_nudge_message("change direction")
    assert "<USER-NUDGE>" in text2
    assert "<USER-INTERRUPT>" not in text2


def test_submit_without_cancel_listener_does_not_explode():
    """submit() must work even if no agent loop has allocated a cancel event."""
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    # No cancel_event call before submit — this is the "first turn" case
    reg.submit("sid-fresh", "hello")
    assert reg.has_pending("sid-fresh") is True


def test_latest_wins_still_holds_after_extension():
    """Adding cancel-event API must not break the original latest-wins semantics."""
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    reg.submit("sid", "first")
    reg.submit("sid", "second")
    assert reg.consume("sid") == "second"


# ---------------------------------------------------------------------------
# Cancel mechanism in isolation (no agent loop coupling)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steer_cancel_unblocks_pending_task():
    """A task awaiting on cancel_event resolves when submit fires."""
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    sid = "sid-cancel-test"
    cancel_event = reg.cancel_event(sid)

    async def waiter() -> str:
        await cancel_event.wait()
        return "cancelled"

    task = asyncio.create_task(waiter())

    async def trigger() -> None:
        await asyncio.sleep(0.05)
        reg.submit(sid, "actually go left instead")

    asyncio.create_task(trigger())

    result = await asyncio.wait_for(task, timeout=2.0)
    assert result == "cancelled"


# ---------------------------------------------------------------------------
# SteerBuffer
# ---------------------------------------------------------------------------


def test_steer_buffer_appends_and_drains():
    """Buffer accumulates messages; drain returns concatenated + clears."""
    from opencomputer.agent.steer import SteerBuffer

    buf = SteerBuffer()
    assert buf.append("sid-1", "first") == 0
    assert buf.append("sid-1", "second") == 0
    drained = buf.drain("sid-1")
    assert "first" in drained
    assert "second" in drained
    assert "\n---\n" in drained
    # Drain clears
    assert buf.drain("sid-1") == ""


def test_steer_buffer_drops_oldest_at_cap():
    """Buffer enforces MAX cap by dropping oldest entries; returns drop count."""
    from opencomputer.agent.steer import SteerBuffer

    buf = SteerBuffer()
    for i in range(SteerBuffer.MAX + 2):
        buf.append("sid-1", f"msg-{i}")
    drained = buf.drain("sid-1")
    # Oldest 2 dropped
    assert "msg-0" not in drained
    assert "msg-1" not in drained
    # Last MAX retained
    assert f"msg-{SteerBuffer.MAX + 1}" in drained
    assert "msg-2" in drained


def test_steer_buffer_drain_empty_returns_empty_string():
    """Draining an unused session returns an empty string, not None."""
    from opencomputer.agent.steer import SteerBuffer

    buf = SteerBuffer()
    assert buf.drain("nonexistent") == ""


def test_steer_buffer_has_pending():
    """``has_pending`` reports buffer state without consuming."""
    from opencomputer.agent.steer import SteerBuffer

    buf = SteerBuffer()
    assert buf.has_pending("sid") is False
    buf.append("sid", "hi")
    assert buf.has_pending("sid") is True
    buf.drain("sid")
    assert buf.has_pending("sid") is False


def test_steer_buffer_thread_safety_smoke():
    """Concurrent appends from threads don't corrupt the buffer."""
    from opencomputer.agent.steer import SteerBuffer

    buf = SteerBuffer()

    def worker(idx: int) -> None:
        for _ in range(10):
            buf.append(f"sid-{idx}", f"msg-from-{idx}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Each session gets MAX retained; no corruption (no exceptions, drain works)
    for i in range(4):
        drained = buf.drain(f"sid-{i}")
        assert "msg-from-" in drained
