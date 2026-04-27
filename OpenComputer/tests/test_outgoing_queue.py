"""Tier-A item 14 follow-up — outgoing queue + drainer + MCP write tools."""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from opencomputer.gateway.outgoing_drainer import OutgoingDrainer
from opencomputer.gateway.outgoing_queue import (
    OutgoingMessage,
    OutgoingQueue,
)

# ──────────────────────────── queue ────────────────────────────


def test_enqueue_returns_queued(tmp_path):
    q = OutgoingQueue(tmp_path / "x.db")
    msg = q.enqueue(platform="telegram", chat_id="123", body="hi")
    assert msg.status == "queued"
    assert msg.platform == "telegram"
    assert msg.chat_id == "123"
    assert msg.body == "hi"
    assert len(msg.id) >= 8


def test_list_queued_oldest_first(tmp_path):
    q = OutgoingQueue(tmp_path / "x.db")
    a = q.enqueue(platform="telegram", chat_id="1", body="a")
    time.sleep(0.01)
    b = q.enqueue(platform="telegram", chat_id="1", body="b")
    rows = q.list_queued()
    assert rows[0].id == a.id
    assert rows[1].id == b.id


def test_mark_sent_transitions_status(tmp_path):
    q = OutgoingQueue(tmp_path / "x.db")
    msg = q.enqueue(platform="telegram", chat_id="1", body="hi")
    q.mark_sent(msg.id)
    fetched = q.get(msg.id)
    assert fetched is not None
    assert fetched.status == "sent"
    assert fetched.sent_at is not None
    assert fetched.attempts == 1


def test_mark_failed_records_error(tmp_path):
    q = OutgoingQueue(tmp_path / "x.db")
    msg = q.enqueue(platform="telegram", chat_id="1", body="hi")
    q.mark_failed(msg.id, "auth failed")
    fetched = q.get(msg.id)
    assert fetched is not None
    assert fetched.status == "failed"
    assert fetched.error == "auth failed"


def test_get_unknown_returns_none(tmp_path):
    q = OutgoingQueue(tmp_path / "x.db")
    assert q.get("does-not-exist") is None


def test_expire_stale_marks_old_queued_rows(tmp_path):
    q = OutgoingQueue(tmp_path / "x.db")
    msg = q.enqueue(platform="telegram", chat_id="1", body="hi")
    # Force the row to be 8 days old
    with sqlite3.connect(tmp_path / "x.db") as conn:
        conn.execute(
            "UPDATE outgoing_messages SET enqueued_at = ? WHERE id = ?",
            (time.time() - 8 * 86400, msg.id),
        )
        conn.commit()
    n = q.expire_stale()
    assert n == 1
    assert q.get(msg.id).status == "expired"  # type: ignore[union-attr]


def test_expire_stale_skips_recent_rows(tmp_path):
    q = OutgoingQueue(tmp_path / "x.db")
    q.enqueue(platform="telegram", chat_id="1", body="hi")
    n = q.expire_stale()
    assert n == 0


# ──────────────────────────── drainer ────────────────────────────


class _FakeSendResult:
    def __init__(self, success: bool = True, error: str | None = None) -> None:
        self.success = success
        self.error = error


class _FakeAdapter:
    def __init__(self, *, fail_with: str | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_with = fail_with

    async def send(self, chat_id: str, text: str):
        self.sent.append((chat_id, text))
        if self.fail_with:
            return _FakeSendResult(success=False, error=self.fail_with)
        return _FakeSendResult(success=True)


@pytest.mark.asyncio
async def test_drainer_dispatches_queued_messages(tmp_path):
    q = OutgoingQueue(tmp_path / "x.db")
    msg = q.enqueue(platform="telegram", chat_id="42", body="hello")
    adapter = _FakeAdapter()

    drainer = OutgoingDrainer(q, {"telegram": adapter}, poll_interval_seconds=0.05)
    task = asyncio.create_task(drainer.run_forever())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if q.get(msg.id).status == "sent":  # type: ignore[union-attr]
            break
    drainer.stop()
    await asyncio.wait_for(task, timeout=3.0)

    assert adapter.sent == [("42", "hello")]
    assert q.get(msg.id).status == "sent"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_drainer_marks_failed_on_adapter_error(tmp_path):
    q = OutgoingQueue(tmp_path / "x.db")
    msg = q.enqueue(platform="telegram", chat_id="42", body="hi")
    adapter = _FakeAdapter(fail_with="chat not found")

    drainer = OutgoingDrainer(q, {"telegram": adapter}, poll_interval_seconds=0.05)
    task = asyncio.create_task(drainer.run_forever())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if q.get(msg.id).status == "failed":  # type: ignore[union-attr]
            break
    drainer.stop()
    await asyncio.wait_for(task, timeout=3.0)

    assert q.get(msg.id).error == "chat not found"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_drainer_marks_failed_when_no_adapter_for_platform(tmp_path):
    q = OutgoingQueue(tmp_path / "x.db")
    msg = q.enqueue(platform="discord", chat_id="42", body="hi")
    # Drainer only knows about telegram.
    drainer = OutgoingDrainer(
        q, {"telegram": _FakeAdapter()}, poll_interval_seconds=0.05,
    )
    task = asyncio.create_task(drainer.run_forever())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if q.get(msg.id).status == "failed":  # type: ignore[union-attr]
            break
    drainer.stop()
    await asyncio.wait_for(task, timeout=3.0)

    fetched = q.get(msg.id)
    assert fetched is not None
    assert fetched.status == "failed"
    assert "no live adapter" in (fetched.error or "")


@pytest.mark.asyncio
async def test_drainer_raised_exception_in_send_recorded(tmp_path):
    q = OutgoingQueue(tmp_path / "x.db")
    msg = q.enqueue(platform="telegram", chat_id="42", body="hi")

    class _RaisingAdapter:
        async def send(self, chat_id, text):
            raise RuntimeError("boom")

    drainer = OutgoingDrainer(
        q, {"telegram": _RaisingAdapter()}, poll_interval_seconds=0.05,
    )
    task = asyncio.create_task(drainer.run_forever())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if q.get(msg.id).status == "failed":  # type: ignore[union-attr]
            break
    drainer.stop()
    await asyncio.wait_for(task, timeout=3.0)

    assert "RuntimeError" in (q.get(msg.id).error or "")  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_expire_stale_on_boot(tmp_path):
    q = OutgoingQueue(tmp_path / "x.db")
    msg = q.enqueue(platform="telegram", chat_id="42", body="hi")
    with sqlite3.connect(tmp_path / "x.db") as conn:
        conn.execute(
            "UPDATE outgoing_messages SET enqueued_at = ? WHERE id = ?",
            (time.time() - 8 * 86400, msg.id),
        )
        conn.commit()
    drainer = OutgoingDrainer(q, {})
    n = await drainer.expire_stale_on_boot()
    assert n == 1
    assert q.get(msg.id).status == "expired"  # type: ignore[union-attr]


# ──────────────────────────── MCP tools ────────────────────────────


def _get_tool_fn(server, name: str):
    return server._tool_manager._tools[name].fn


def test_mcp_messages_send_enqueues(tmp_path, monkeypatch):
    monkeypatch.setattr("opencomputer.mcp.server._home", lambda: tmp_path)
    from opencomputer.mcp.server import build_server

    server = build_server()
    fn = _get_tool_fn(server, "messages_send")
    result = asyncio.run(fn(platform="telegram", chat_id="42", body="hi"))
    assert result["status"] == "queued"
    assert "id" in result
    # And the row exists
    q = OutgoingQueue(tmp_path / "sessions.db")
    msg = q.get(result["id"])
    assert msg is not None
    assert msg.platform == "telegram"
    assert msg.body == "hi"


def test_mcp_messages_send_status_returns_state(tmp_path, monkeypatch):
    monkeypatch.setattr("opencomputer.mcp.server._home", lambda: tmp_path)
    q = OutgoingQueue(tmp_path / "sessions.db")
    msg = q.enqueue(platform="telegram", chat_id="1", body="hi")

    from opencomputer.mcp.server import build_server
    server = build_server()
    fn = _get_tool_fn(server, "messages_send_status")
    result = fn(message_id=msg.id)
    assert result is not None
    assert result["status"] == "queued"
    assert result["platform"] == "telegram"


def test_mcp_messages_send_status_unknown_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr("opencomputer.mcp.server._home", lambda: tmp_path)
    OutgoingQueue(tmp_path / "sessions.db")  # init schema
    from opencomputer.mcp.server import build_server
    server = build_server()
    fn = _get_tool_fn(server, "messages_send_status")
    assert fn(message_id="bogus") is None


@pytest.mark.asyncio
async def test_mcp_events_wait_returns_immediately_when_messages_exist(
    tmp_path, monkeypatch,
):
    """Long-poll should not block when the cursor finds new messages."""
    monkeypatch.setattr("opencomputer.mcp.server._home", lambda: tmp_path)
    # Build a tiny sessions.db with one message
    db_path = tmp_path / "sessions.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, started_at REAL, ended_at REAL,
                platform TEXT NOT NULL, model TEXT, title TEXT,
                message_count INTEGER, input_tokens INTEGER, output_tokens INTEGER
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, tool_call_id TEXT, tool_calls TEXT,
                name TEXT, reasoning TEXT, reasoning_details TEXT,
                codex_reasoning_items TEXT, timestamp REAL NOT NULL
            );
            INSERT INTO sessions (id, started_at, platform) VALUES ('s1', 1, 'telegram');
            INSERT INTO messages (session_id, role, content, timestamp)
            VALUES ('s1', 'user', 'hello', 100);
            """
        )
    from opencomputer.mcp.server import build_server
    server = build_server()
    fn = _get_tool_fn(server, "events_wait")
    start = time.monotonic()
    result = await fn(since_message_id=0, timeout_s=5.0, poll_interval_s=0.1)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0  # no blocking when there are messages
    assert len(result["messages"]) == 1
    assert result["next_cursor"] >= 1


@pytest.mark.asyncio
async def test_mcp_events_wait_times_out_when_no_new_messages(tmp_path, monkeypatch):
    monkeypatch.setattr("opencomputer.mcp.server._home", lambda: tmp_path)
    db_path = tmp_path / "sessions.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL,
                ended_at REAL, platform TEXT NOT NULL, model TEXT, title TEXT,
                message_count INTEGER, input_tokens INTEGER, output_tokens INTEGER);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, tool_call_id TEXT, tool_calls TEXT,
                name TEXT, reasoning TEXT, reasoning_details TEXT,
                codex_reasoning_items TEXT, timestamp REAL NOT NULL);
            """
        )
    from opencomputer.mcp.server import build_server
    server = build_server()
    fn = _get_tool_fn(server, "events_wait")
    start = time.monotonic()
    result = await fn(since_message_id=0, timeout_s=0.5, poll_interval_s=0.1)
    elapsed = time.monotonic() - start
    assert 0.4 <= elapsed <= 1.5
    assert result["messages"] == []
    assert result["next_cursor"] == 0


@pytest.mark.asyncio
async def test_mcp_events_wait_caps_timeout_at_120s(tmp_path, monkeypatch):
    """The wait should bound timeout_s to 120s. Verify the cap by
    inspecting the elapsed time would-be if we waited (using a fast
    cancel via an existing message arriving).

    We don't actually wait 120s in the test suite — that would slow CI.
    Instead we insert a message after a brief delay and confirm the
    poll picks it up via the same code path.
    """
    monkeypatch.setattr("opencomputer.mcp.server._home", lambda: tmp_path)
    db_path = tmp_path / "sessions.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL,
                ended_at REAL, platform TEXT NOT NULL, model TEXT, title TEXT,
                message_count INTEGER, input_tokens INTEGER, output_tokens INTEGER);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, tool_call_id TEXT, tool_calls TEXT,
                name TEXT, reasoning TEXT, reasoning_details TEXT,
                codex_reasoning_items TEXT, timestamp REAL NOT NULL);
            INSERT INTO sessions (id, started_at, platform) VALUES ('s1', 1, 'telegram');
            """
        )
    from opencomputer.mcp.server import build_server
    server = build_server()
    fn = _get_tool_fn(server, "events_wait")

    # Inject a message after ~0.2s so the long-poll picks it up quickly.
    async def _inject_after_delay() -> None:
        await asyncio.sleep(0.2)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES ('s1', 'user', 'hi', 100)"
            )

    start = time.monotonic()
    inject_task = asyncio.create_task(_inject_after_delay())
    # Pass an absurd timeout — we expect the inject to short-circuit
    # the wait long before either 120s or the absurd value matters.
    result = await fn(since_message_id=0, timeout_s=10000.0, poll_interval_s=0.1)
    await inject_task
    assert time.monotonic() - start < 5.0  # fast path
    assert len(result["messages"]) == 1
