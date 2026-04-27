"""Tier-A item 21 — thread_hint-aware session id resolution."""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.gateway.dispatch import session_id_for
from plugin_sdk.core import MessageEvent, Platform

# ──────────────────────────── public helper ────────────────────────────


def test_session_id_no_hint_matches_legacy():
    """Without thread_hint the id is unchanged from before Item 21."""
    no_hint = session_id_for("telegram", "12345")
    explicit_none = session_id_for("telegram", "12345", thread_hint=None)
    explicit_empty = session_id_for("telegram", "12345", thread_hint="")
    assert no_hint == explicit_none == explicit_empty


def test_session_id_thread_hint_diverges_from_default():
    base = session_id_for("telegram", "12345")
    cron = session_id_for("telegram", "12345", thread_hint="cron:morning")
    assert base != cron


def test_session_id_different_hints_produce_different_ids():
    a = session_id_for("telegram", "12345", thread_hint="cron:morning")
    b = session_id_for("telegram", "12345", thread_hint="cron:evening")
    assert a != b


def test_session_id_same_hint_is_stable():
    a = session_id_for("telegram", "12345", thread_hint="cron:morning")
    b = session_id_for("telegram", "12345", thread_hint="cron:morning")
    assert a == b


def test_session_id_per_platform_isolation_with_hints():
    tg = session_id_for("telegram", "12345", thread_hint="x")
    dc = session_id_for("discord", "12345", thread_hint="x")
    assert tg != dc


# ──────────────────────────── dispatcher uses metadata ────────────────────────────


def test_dispatch_session_id_for_no_metadata(no_dispatch_imports):
    """No metadata → legacy behavior."""
    from opencomputer.gateway.dispatch import Dispatch

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="42",
        user_id="42",
        text="hi",
        timestamp=0.0,
    )
    d = Dispatch.__new__(Dispatch)  # skip __init__
    sid = d._session_id_for(event)
    assert sid == session_id_for("telegram", "42")


def test_dispatch_session_id_for_with_thread_hint(no_dispatch_imports):
    from opencomputer.gateway.dispatch import Dispatch

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="42",
        user_id="42",
        text="hi",
        timestamp=0.0,
        metadata={"thread_hint": "cron:morning-briefing"},
    )
    d = Dispatch.__new__(Dispatch)
    sid = d._session_id_for(event)
    assert sid == session_id_for("telegram", "42", "cron:morning-briefing")
    # And it differs from the default.
    assert sid != session_id_for("telegram", "42")


def test_dispatch_session_id_for_strips_whitespace_hint(no_dispatch_imports):
    from opencomputer.gateway.dispatch import Dispatch

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="42",
        user_id="42",
        text="hi",
        timestamp=0.0,
        metadata={"thread_hint": "  cron:morning  "},
    )
    d = Dispatch.__new__(Dispatch)
    sid = d._session_id_for(event)
    assert sid == session_id_for("telegram", "42", "cron:morning")


def test_dispatch_session_id_for_ignores_blank_hint(no_dispatch_imports):
    from opencomputer.gateway.dispatch import Dispatch

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="42",
        user_id="42",
        text="hi",
        timestamp=0.0,
        metadata={"thread_hint": "   "},
    )
    d = Dispatch.__new__(Dispatch)
    sid = d._session_id_for(event)
    # Blank hint = no hint = legacy.
    assert sid == session_id_for("telegram", "42")


def test_dispatch_session_id_for_ignores_non_string_hint(no_dispatch_imports):
    from opencomputer.gateway.dispatch import Dispatch

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="42",
        user_id="42",
        text="hi",
        timestamp=0.0,
        metadata={"thread_hint": 12345},  # type: ignore[dict-item]
    )
    d = Dispatch.__new__(Dispatch)
    sid = d._session_id_for(event)
    assert sid == session_id_for("telegram", "42")


# ──────────────────────────── messages_send carries hint ────────────────────────────


@pytest.mark.asyncio
async def test_messages_send_carries_thread_hint_into_queue(tmp_path, monkeypatch):
    """The MCP messages_send tool persists thread_hint into the queue
    metadata so the drainer / future routing can read it."""
    monkeypatch.setattr("opencomputer.mcp.server._home", lambda: tmp_path)
    from opencomputer.gateway.outgoing_queue import OutgoingQueue
    from opencomputer.mcp.server import build_server

    server = build_server()
    fn = server._tool_manager._tools["messages_send"].fn
    result = await fn(
        platform="telegram",
        chat_id="42",
        body="morning briefing",
        thread_hint="cron:morning-briefing",
    )
    assert result["thread_hint"] == "cron:morning-briefing"
    queue = OutgoingQueue(tmp_path / "sessions.db")
    msg = queue.get(result["id"])
    assert msg is not None
    assert msg.metadata.get("thread_hint") == "cron:morning-briefing"


@pytest.mark.asyncio
async def test_messages_send_no_hint_omits_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("opencomputer.mcp.server._home", lambda: tmp_path)
    from opencomputer.gateway.outgoing_queue import OutgoingQueue
    from opencomputer.mcp.server import build_server

    server = build_server()
    fn = server._tool_manager._tools["messages_send"].fn
    result = await fn(platform="telegram", chat_id="42", body="hi")
    assert result["thread_hint"] is None
    queue = OutgoingQueue(tmp_path / "sessions.db")
    msg = queue.get(result["id"])
    assert msg is not None
    assert "thread_hint" not in msg.metadata


# ──────────────────────────── fixtures ────────────────────────────


@pytest.fixture
def no_dispatch_imports():
    """The Dispatch class touches AgentLoop on full construction; we
    use ``__new__`` to skip ``__init__`` for these unit tests."""
    yield
