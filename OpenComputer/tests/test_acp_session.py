"""PR-D: ACPSession lifecycle."""
from __future__ import annotations

import pytest

from opencomputer.acp.session import ACPSession


@pytest.mark.asyncio
async def test_session_construct():
    sent = []
    s = ACPSession(session_id="test-1", send=lambda m, p: sent.append((m, p)))
    assert s.session_id == "test-1"


@pytest.mark.asyncio
async def test_cancel_sets_event():
    sent = []
    s = ACPSession(session_id="test-2", send=lambda m, p: sent.append((m, p)))
    # Not running → cancel returns False
    result = await s.cancel()
    # First call returns True (event was unset → flips to set)
    # Second call returns False (was already set)
    second = await s.cancel()
    assert result != second  # one True, one False


@pytest.mark.asyncio
async def test_load_from_db_missing_returns_false():
    sent = []
    s = ACPSession(session_id="missing-session-xyz", send=lambda m, p: sent.append((m, p)))
    # No such session in DB → returns False
    loaded = await s.load_from_db()
    assert loaded is False


def _make_session(session_id="s1"):
    notifications = []

    def send(method, params):
        notifications.append((method, params))

    sess = ACPSession(session_id=session_id, send=send)
    return sess, notifications


def test_emit_event_sends_notification():
    sess, notifications = _make_session()
    sess.emit_event("session/toolStart", {"tool_name": "Read"})
    assert len(notifications) == 1
    assert notifications[0] == ("session/toolStart", {"tool_name": "Read"})


@pytest.mark.asyncio
async def test_event_queue_receives_emitted_events():
    sess, _ = _make_session()
    sess.emit_event("session/toolComplete", {"tool_call_id": "x"})
    assert not sess.event_queue.empty()
    event = sess.event_queue.get_nowait()
    assert event["method"] == "session/toolComplete"
