"""CC §11 — /checkpoint and /restore slash commands.

Round-trip: /checkpoint saves a labeled snapshot of the live message
list; /restore (by id, label, or unique prefix) rewinds the session's
DB messages to that snapshot. Spec:
docs/OC-FROM-CLAUDE-CODE.md §11.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from opencomputer.agent.slash_commands_impl.checkpoint_cmd import CheckpointCommand
from opencomputer.agent.slash_commands_impl.restore_cmd import RestoreCommand
from opencomputer.agent.state import SessionDB
from plugin_sdk.runtime_context import RuntimeContext


def _fresh_db() -> SessionDB:
    return SessionDB(Path(tempfile.mkdtemp()) / "t.db")


def _seed_session_with_messages(db: SessionDB, n: int = 3) -> str:
    sid = db.allocate_session_id()
    db.create_session(sid, platform="cli", model="m")
    # Persist a few messages via the same path the loop uses.
    from plugin_sdk.core import Message
    for i in range(n):
        db.append_message(
            sid, Message(role="user" if i % 2 == 0 else "assistant", content=f"msg-{i}")
        )
    return sid


def _runtime_for(db: SessionDB, sid: str, **extra) -> RuntimeContext:
    return RuntimeContext(
        custom={"session_id": sid, "session_db": db, **extra}
    )


# ─── /checkpoint ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_checkpoint_with_label_saves_to_db():
    db = _fresh_db()
    sid = _seed_session_with_messages(db, n=3)
    rt = _runtime_for(db, sid)
    result = await CheckpointCommand().execute("before-refactor", rt)
    assert result.handled
    # The created checkpoint should be listable.
    rows = db.list_prompt_checkpoints(sid)
    assert len(rows) == 1
    assert rows[0].label == "before-refactor"
    assert "before-refactor" in result.output


@pytest.mark.asyncio
async def test_checkpoint_with_no_label_auto_labels():
    db = _fresh_db()
    sid = _seed_session_with_messages(db, n=1)
    rt = _runtime_for(db, sid)
    result = await CheckpointCommand().execute("", rt)
    rows = db.list_prompt_checkpoints(sid)
    assert len(rows) == 1
    assert rows[0].label.startswith("auto-")


@pytest.mark.asyncio
async def test_checkpoint_label_truncated_to_80_chars():
    db = _fresh_db()
    sid = _seed_session_with_messages(db, n=1)
    rt = _runtime_for(db, sid)
    long_label = "x" * 200
    await CheckpointCommand().execute(long_label, rt)
    rows = db.list_prompt_checkpoints(sid)
    assert len(rows[0].label) == 80


@pytest.mark.asyncio
async def test_checkpoint_without_session_id_warns():
    rt = RuntimeContext(custom={})
    result = await CheckpointCommand().execute("x", rt)
    assert "no active session" in result.output.lower() or "session_db" in result.output.lower()


@pytest.mark.asyncio
async def test_checkpoint_with_empty_session_returns_helpful_message():
    db = _fresh_db()
    sid = db.allocate_session_id()
    db.create_session(sid, platform="cli", model="m")
    # No messages appended.
    rt = _runtime_for(db, sid)
    result = await CheckpointCommand().execute("x", rt)
    assert "no messages" in result.output.lower()
    assert db.list_prompt_checkpoints(sid) == []


@pytest.mark.asyncio
async def test_checkpoint_prefers_in_flight_messages_over_db():
    """When the loop plumbs ``current_messages`` into runtime.custom,
    the checkpoint captures THAT list (not the persisted DB rows)."""
    db = _fresh_db()
    sid = _seed_session_with_messages(db, n=2)  # 2 in DB
    in_flight = [
        {"role": "user", "content": "live-1"},
        {"role": "assistant", "content": "live-2"},
        {"role": "user", "content": "live-3"},
    ]
    rt = _runtime_for(db, sid, current_messages=in_flight)
    await CheckpointCommand().execute("live", rt)
    rows = db.list_prompt_checkpoints(sid)
    assert len(rows) == 1
    cp = rows[0]
    # 3 live messages captured (not the 2 DB rows)
    assert len(cp.messages) == 3
    assert cp.messages[0]["content"] == "live-1"


# ─── /restore ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_no_args_lists_recent_checkpoints():
    db = _fresh_db()
    sid = _seed_session_with_messages(db, n=1)
    db.create_prompt_checkpoint(
        session_id=sid, prompt_index=1, messages=[{"role": "user", "content": "x"}],
        label="alpha",
    )
    rt = _runtime_for(db, sid)
    result = await RestoreCommand().execute("", rt)
    assert "alpha" in result.output


@pytest.mark.asyncio
async def test_restore_no_args_with_no_checkpoints():
    db = _fresh_db()
    sid = _seed_session_with_messages(db, n=1)
    rt = _runtime_for(db, sid)
    result = await RestoreCommand().execute("", rt)
    assert "no checkpoints" in result.output.lower()


@pytest.mark.asyncio
async def test_restore_by_full_id_rewinds_messages():
    db = _fresh_db()
    sid = _seed_session_with_messages(db, n=5)
    # Snapshot a smaller list.
    cp_id = db.create_prompt_checkpoint(
        session_id=sid,
        prompt_index=2,
        messages=[
            {"role": "user", "content": "early-1"},
            {"role": "assistant", "content": "early-2"},
        ],
        label="early",
    )
    rt = _runtime_for(db, sid)
    result = await RestoreCommand().execute(cp_id, rt)
    assert "restored" in result.output.lower()
    # The DB messages should now be the snapshot, not the 5 original.
    msgs = db.get_messages(sid)
    assert len(msgs) == 2
    assert msgs[0].content == "early-1"


@pytest.mark.asyncio
async def test_restore_by_label_picks_most_recent():
    db = _fresh_db()
    sid = _seed_session_with_messages(db, n=1)
    db.create_prompt_checkpoint(
        session_id=sid, prompt_index=1, messages=[{"role": "user", "content": "first"}],
        label="ABC",
    )
    cp_id_2 = db.create_prompt_checkpoint(
        session_id=sid, prompt_index=2, messages=[{"role": "user", "content": "second"}],
        label="ABC",
    )
    rt = _runtime_for(db, sid)
    result = await RestoreCommand().execute("ABC", rt)
    assert "restored" in result.output.lower()
    msgs = db.get_messages(sid)
    # Most recent ("second") wins.
    assert len(msgs) == 1
    assert msgs[0].content == "second"
    assert cp_id_2[:8] in result.output


@pytest.mark.asyncio
async def test_restore_by_unique_id_prefix():
    db = _fresh_db()
    sid = _seed_session_with_messages(db, n=1)
    cp_id = db.create_prompt_checkpoint(
        session_id=sid, prompt_index=1,
        messages=[{"role": "user", "content": "snap"}], label="x",
    )
    rt = _runtime_for(db, sid)
    result = await RestoreCommand().execute(cp_id[:8], rt)
    assert "restored" in result.output.lower()


@pytest.mark.asyncio
async def test_restore_unknown_arg_helpful_message():
    db = _fresh_db()
    sid = _seed_session_with_messages(db, n=1)
    rt = _runtime_for(db, sid)
    result = await RestoreCommand().execute("nope-nope", rt)
    assert "no checkpoint matches" in result.output.lower()


@pytest.mark.asyncio
async def test_restore_refuses_cross_session():
    """A checkpoint id from session A cannot restore session B."""
    db = _fresh_db()
    sid_a = _seed_session_with_messages(db, n=2)
    sid_b = _seed_session_with_messages(db, n=3)
    cp_id = db.create_prompt_checkpoint(
        session_id=sid_a, prompt_index=1,
        messages=[{"role": "user", "content": "a"}], label="a",
    )
    rt = _runtime_for(db, sid_b)
    result = await RestoreCommand().execute(cp_id, rt)
    assert "different session" in result.output.lower() or "cross" in result.output.lower()
    # Session B's messages stay intact.
    assert len(db.get_messages(sid_b)) == 3


@pytest.mark.asyncio
async def test_restore_without_session_id_warns():
    rt = RuntimeContext(custom={})
    result = await RestoreCommand().execute("x", rt)
    assert "session" in result.output.lower()


@pytest.mark.asyncio
async def test_full_roundtrip_checkpoint_then_restore():
    db = _fresh_db()
    sid = _seed_session_with_messages(db, n=3)
    # Snapshot here (at the 3-message mark).
    rt = _runtime_for(db, sid)
    await CheckpointCommand().execute("snap1", rt)

    # Add more messages.
    from plugin_sdk.core import Message
    db.append_message(sid, Message(role="user", content="msg-3"))
    db.append_message(sid, Message(role="assistant", content="msg-4"))
    assert len(db.get_messages(sid)) == 5

    # Restore back to the 3-message snapshot.
    result = await RestoreCommand().execute("snap1", rt)
    assert "restored" in result.output.lower()
    msgs = db.get_messages(sid)
    assert len(msgs) == 3


@pytest.mark.asyncio
async def test_restore_ambiguous_prefix_message():
    db = _fresh_db()
    sid = _seed_session_with_messages(db, n=1)
    # Two checkpoints — we'll force prefix overlap via the same UUID
    # prefix. UUIDs are random — search the listing for two we can
    # share a 1-char prefix on. Cheap path: insert with manual ids.
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO prompt_checkpoints "
            "(id, session_id, prompt_index, messages_snapshot_json, label, created_at) "
            "VALUES ('aaaa-1111-2222-3333-444444444444', ?, 1, '[]', 'L1', 1.0)",
            (sid,),
        )
        conn.execute(
            "INSERT INTO prompt_checkpoints "
            "(id, session_id, prompt_index, messages_snapshot_json, label, created_at) "
            "VALUES ('aaaa-5555-6666-7777-888888888888', ?, 2, '[]', 'L2', 2.0)",
            (sid,),
        )
    rt = _runtime_for(db, sid)
    result = await RestoreCommand().execute("aaaa", rt)
    assert "matches" in result.output.lower() and "specific" in result.output.lower()
