"""Tests for AgentLoop lazy session creation — Wave 5 T17 closure.

Verifies the loop no longer eagerly writes a session row at the top of
``run_conversation``; the row is created on demand when a message
persistence call (``_persist_message`` / ``_persist_messages_batch``)
fires for the first time on a fresh session id.

This closes the deferral noted in `project_hermes_wave5_done.md`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.config import Config, ModelConfig, SessionConfig
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BaseProvider


class _StubProvider(BaseProvider):
    """Minimal BaseProvider — never invoked by these tests; AgentLoop
    accepts it at construction so we can test the persistence helpers
    without a real LLM."""

    name = "stub"

    async def complete(self, **kwargs):  # type: ignore[override]
        raise NotImplementedError

    async def stream_complete(self, **kwargs):  # type: ignore[override]
        raise NotImplementedError


def _make_loop(tmp_path: Path) -> AgentLoop:
    """Construct an AgentLoop with a fresh per-test SessionDB."""
    cfg = Config(
        model=ModelConfig(model="anthropic:claude-opus-4-7"),
        session=SessionConfig(db_path=tmp_path / "lazy.db"),
    )
    return AgentLoop(provider=_StubProvider(), config=cfg)


def test_ensure_session_persisted_idempotent(tmp_path):
    loop = _make_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop._pending_session_meta[sid] = {"platform": "cli", "model": "x", "cwd": "/tmp"}
    assert sid not in loop._session_ensured
    loop._ensure_session_persisted(sid)
    assert sid in loop._session_ensured
    # Second call is a no-op (no DB INSERT issued)
    rows_before = len(loop.db.list_sessions(limit=100))
    loop._ensure_session_persisted(sid)
    loop._ensure_session_persisted(sid)
    rows_after = len(loop.db.list_sessions(limit=100))
    assert rows_before == rows_after


def test_persist_message_creates_row_on_first_call(tmp_path):
    loop = _make_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop._pending_session_meta[sid] = {"platform": "cli", "model": "x", "cwd": "/tmp"}
    # Before any persist call: no row
    assert all(r["id"] != sid for r in loop.db.list_sessions(limit=100))
    # First _persist_message creates it
    loop._persist_message(sid, Message(role="user", content="hi"))
    rows = [r for r in loop.db.list_sessions(limit=100) if r["id"] == sid]
    assert len(rows) == 1


def test_persist_messages_batch_creates_row_once(tmp_path):
    loop = _make_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop._pending_session_meta[sid] = {"platform": "cli", "model": "x", "cwd": "/tmp"}
    assert sid not in loop._session_ensured
    loop._persist_messages_batch(
        sid,
        [Message(role="user", content="a"), Message(role="user", content="b")],
    )
    rows = [r for r in loop.db.list_sessions(limit=100) if r["id"] == sid]
    assert len(rows) == 1
    assert sid in loop._session_ensured


def test_existing_session_marked_ensured(tmp_path):
    """Loop must mark resumed sessions as ensured so the next persist skips ensure_session."""
    loop = _make_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    # Pre-create the row (resumed session path)
    loop.db.create_session(sid, platform="cli")
    # Now imagine run_conversation entry on this existing session:
    loop._session_ensured.add(sid)  # this is what loop.py does on the existing path
    rows_before = len(loop.db.list_sessions(limit=100))
    loop._persist_message(sid, Message(role="user", content="hi"))
    rows_after = len(loop.db.list_sessions(limit=100))
    # No NEW session row created; existing one was used
    assert rows_after == rows_before


def test_pending_meta_used_on_lazy_create(tmp_path):
    """The platform/model/cwd captured at run_conversation entry survives to ensure."""
    loop = _make_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop._pending_session_meta[sid] = {
        "platform": "telegram",
        "model": "anthropic:opus-4-7",
        "cwd": "/tmp/work",
    }
    loop._persist_message(sid, Message(role="user", content="hi"))
    row = loop.db.get_session(sid)
    assert row is not None
    assert row["platform"] == "telegram"
    assert row["model"] == "anthropic:opus-4-7"
    assert row["cwd"] == "/tmp/work"


def test_no_pending_meta_falls_back_to_defaults(tmp_path):
    """If somehow _persist_message fires for an sid never seeded into
    pending_session_meta (e.g. test/harness path), defaults still apply."""
    loop = _make_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    # Skip pending_session_meta seeding entirely
    loop._persist_message(sid, Message(role="user", content="orphan"))
    row = loop.db.get_session(sid)
    assert row is not None
    assert row["platform"] == "cli"  # default
