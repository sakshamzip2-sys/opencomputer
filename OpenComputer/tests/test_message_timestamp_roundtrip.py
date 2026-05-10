"""``Message.timestamp`` roundtrips through SessionDB + cache-ttl uses it.

Closes the M6 gap I admitted: cache-ttl mode was theoretically
complete but produced no real-world effect because plugin_sdk.core.Message
had no timestamp field. As of 2026-05-11 the field exists; this test
proves the producer→DB→loader→pruner pipeline carries it end-to-end.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from opencomputer.agent.context_pruning import (
    ContextPruningConfig,
    prune_messages,
)
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message


def _new_session_db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "sessions.db")


def test_message_timestamp_field_defaults_to_none():
    msg = Message(role="user", content="hi")
    assert msg.timestamp is None


def test_message_timestamp_accepts_float():
    msg = Message(role="user", content="hi", timestamp=12345.6)
    assert msg.timestamp == 12345.6


def test_session_db_persists_message_timestamp_from_producer(tmp_path: Path):
    """When the producer attaches a timestamp, SessionDB stores that
    exact value instead of overwriting with time.time()."""
    db = _new_session_db(tmp_path)
    session_id = "test-session"
    db.create_session(session_id=session_id, title="t")
    fixed_ts = 1_700_000_000.0
    db.append_message(session_id, Message(
        role="user", content="hello", timestamp=fixed_ts,
    ))
    msgs = db.get_messages(session_id)
    assert len(msgs) == 1
    assert msgs[0].timestamp == fixed_ts


def test_session_db_defaults_to_wall_clock_when_no_timestamp(tmp_path: Path):
    """Legacy producers that don't set timestamp keep the previous
    behavior — SessionDB stamps with time.time() at append."""
    db = _new_session_db(tmp_path)
    session_id = "test-session"
    db.create_session(session_id=session_id, title="t")
    before = time.time()
    db.append_message(session_id, Message(role="user", content="hello"))
    after = time.time()
    msgs = db.get_messages(session_id)
    assert len(msgs) == 1
    assert msgs[0].timestamp is not None
    assert before <= msgs[0].timestamp <= after


def test_cache_ttl_prunes_real_messages_after_db_roundtrip(tmp_path: Path):
    """End-to-end: producer→SessionDB→loader→pruner — the producer's
    timestamp survives the roundtrip and cache-ttl honours it."""
    db = _new_session_db(tmp_path)
    session_id = "test-session"
    db.create_session(session_id=session_id, title="t")
    now = 1_700_000_000.0
    db.append_message(session_id, Message(
        role="user", content="old", timestamp=now - 3600,
    ))
    db.append_message(session_id, Message(
        role="user", content="fresh", timestamp=now - 10,
    ))
    loaded = db.get_messages(session_id)
    assert len(loaded) == 2
    pruned = prune_messages(
        loaded,
        ContextPruningConfig(mode="cache-ttl", ttl_seconds=60),
        now=now,
    )
    contents = [m.content for m in pruned]
    assert "fresh" in contents
    assert "old" not in contents


def test_cache_ttl_no_op_on_messages_with_recent_timestamps(tmp_path: Path):
    """All-recent → nothing pruned."""
    db = _new_session_db(tmp_path)
    session_id = "test-session"
    db.create_session(session_id=session_id, title="t")
    now = 1_700_000_000.0
    for i in range(5):
        db.append_message(session_id, Message(
            role="user", content=f"m{i}", timestamp=now - 10 - i,
        ))
    loaded = db.get_messages(session_id)
    pruned = prune_messages(
        loaded,
        ContextPruningConfig(mode="cache-ttl", ttl_seconds=3600),
        now=now,
    )
    assert len(pruned) == 5


def test_get_messages_robust_to_zero_timestamp(tmp_path: Path):
    """The schema enforces NOT NULL on ``timestamp``, but a producer
    that passes ``timestamp=0.0`` should still load cleanly (and the
    pruner treats 0.0 as ancient — equivalent to UNIX epoch)."""
    db = _new_session_db(tmp_path)
    session_id = "test-zero"
    db.create_session(session_id=session_id, title="t")
    db.append_message(session_id, Message(
        role="user", content="ancient", timestamp=0.0,
    ))
    msgs = db.get_messages(session_id)
    assert msgs[0].timestamp == 0.0
