"""CC §11 — user-invocable session checkpoints.

The ``prompt_checkpoints`` table has shipped since v15 (2026-05-09) but
no user-facing surface exists to write or restore from it. This adds
the SessionDB helpers backing the ``/checkpoint`` and ``/restore``
slash commands.

Spec: docs/OC-FROM-CLAUDE-CODE.md §11.

Coverage:
  - ``create_prompt_checkpoint`` writes a row with id / label / messages
  - ``list_prompt_checkpoints`` returns most-recent-first
  - ``get_prompt_checkpoint`` by id and by label (label is non-unique;
    returns most recent if multiple)
  - ``delete_prompt_checkpoint`` removes a row by id
  - Adversarial: empty session id, unknown label, malformed JSON
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from opencomputer.agent.state import (
    PromptCheckpoint,
    SessionDB,
)


def _fresh_db() -> SessionDB:
    return SessionDB(Path(tempfile.mkdtemp()) / "t.db")


def _new_session(db: SessionDB) -> str:
    sid = db.allocate_session_id()
    db.create_session(sid, platform="cli", model="m")
    return sid


def test_create_prompt_checkpoint_writes_row():
    db = _fresh_db()
    sid = _new_session(db)
    msgs = [{"role": "user", "content": "hi"}]
    cp_id = db.create_prompt_checkpoint(
        session_id=sid,
        prompt_index=3,
        messages=msgs,
        label="before-refactor",
    )
    assert isinstance(cp_id, str) and len(cp_id) >= 8


def test_list_prompt_checkpoints_returns_most_recent_first():
    db = _fresh_db()
    sid = _new_session(db)
    db.create_prompt_checkpoint(session_id=sid, prompt_index=1, messages=[], label="a")
    time.sleep(0.01)
    db.create_prompt_checkpoint(session_id=sid, prompt_index=2, messages=[], label="b")
    time.sleep(0.01)
    db.create_prompt_checkpoint(session_id=sid, prompt_index=3, messages=[], label="c")

    rows = db.list_prompt_checkpoints(sid)
    assert len(rows) == 3
    assert [r.label for r in rows] == ["c", "b", "a"]


def test_list_prompt_checkpoints_unknown_session_returns_empty():
    db = _fresh_db()
    assert db.list_prompt_checkpoints("nope") == []


def test_list_prompt_checkpoints_empty_session_id_returns_empty():
    db = _fresh_db()
    assert db.list_prompt_checkpoints("") == []


def test_list_prompt_checkpoints_limit():
    db = _fresh_db()
    sid = _new_session(db)
    for i in range(5):
        db.create_prompt_checkpoint(
            session_id=sid, prompt_index=i, messages=[], label=f"cp-{i}"
        )
    rows = db.list_prompt_checkpoints(sid, limit=2)
    assert len(rows) == 2


def test_get_prompt_checkpoint_by_id():
    db = _fresh_db()
    sid = _new_session(db)
    cp_id = db.create_prompt_checkpoint(
        session_id=sid,
        prompt_index=7,
        messages=[{"role": "user", "content": "hi"}],
        label="orig",
    )
    cp = db.get_prompt_checkpoint(cp_id)
    assert cp is not None
    assert isinstance(cp, PromptCheckpoint)
    assert cp.id == cp_id
    assert cp.session_id == sid
    assert cp.prompt_index == 7
    assert cp.label == "orig"
    assert cp.messages == [{"role": "user", "content": "hi"}]


def test_get_prompt_checkpoint_unknown_id_returns_none():
    db = _fresh_db()
    assert db.get_prompt_checkpoint("nope") is None


def test_get_prompt_checkpoint_by_label_in_session_picks_most_recent():
    """Labels are non-unique. Look up by (session_id, label) returns
    the most recently created row."""
    db = _fresh_db()
    sid = _new_session(db)
    db.create_prompt_checkpoint(session_id=sid, prompt_index=1, messages=[], label="A")
    time.sleep(0.01)
    cp_id_new = db.create_prompt_checkpoint(
        session_id=sid, prompt_index=2, messages=[], label="A"
    )
    cp = db.find_prompt_checkpoint_by_label(session_id=sid, label="A")
    assert cp is not None and cp.id == cp_id_new


def test_find_prompt_checkpoint_by_label_unknown_returns_none():
    db = _fresh_db()
    sid = _new_session(db)
    assert db.find_prompt_checkpoint_by_label(session_id=sid, label="never") is None


def test_delete_prompt_checkpoint_removes_row():
    db = _fresh_db()
    sid = _new_session(db)
    cp_id = db.create_prompt_checkpoint(
        session_id=sid, prompt_index=1, messages=[], label="kill"
    )
    deleted = db.delete_prompt_checkpoint(cp_id)
    assert deleted is True
    assert db.get_prompt_checkpoint(cp_id) is None


def test_delete_prompt_checkpoint_unknown_returns_false():
    db = _fresh_db()
    assert db.delete_prompt_checkpoint("nope") is False


def test_create_prompt_checkpoint_serializes_messages_as_json():
    """Round-trip: messages stored as JSON, read back as list[dict]."""
    db = _fresh_db()
    sid = _new_session(db)
    msgs = [
        {"role": "user", "content": "what's up?"},
        {"role": "assistant", "content": "all is well", "tool_calls": []},
    ]
    cp_id = db.create_prompt_checkpoint(
        session_id=sid, prompt_index=1, messages=msgs, label="x"
    )
    cp = db.get_prompt_checkpoint(cp_id)
    assert cp is not None
    assert cp.messages == msgs


def test_create_prompt_checkpoint_rejects_empty_session_id():
    """Empty session id is a caller bug — refuse rather than write a
    dangling row."""
    db = _fresh_db()
    import pytest
    with pytest.raises(ValueError):
        db.create_prompt_checkpoint(
            session_id="", prompt_index=1, messages=[], label="x"
        )


def test_create_prompt_checkpoint_rejects_empty_label():
    """Label is the human handle — empty is a caller bug."""
    db = _fresh_db()
    sid = _new_session(db)
    import pytest
    with pytest.raises(ValueError):
        db.create_prompt_checkpoint(
            session_id=sid, prompt_index=1, messages=[], label=""
        )


def test_get_prompt_checkpoint_with_malformed_json_returns_none_gracefully():
    """A corrupt messages_snapshot_json column shouldn't crash the
    helper — log + return None."""
    db = _fresh_db()
    sid = _new_session(db)
    cp_id = "corrupt-row"
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO prompt_checkpoints "
            "(id, session_id, prompt_index, messages_snapshot_json, label, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cp_id, sid, 1, "not-json{{{", "broken", time.time()),
        )
    # Helper logs warning + returns None rather than raising.
    assert db.get_prompt_checkpoint(cp_id) is None


def test_two_sessions_checkpoints_independent():
    db = _fresh_db()
    a = _new_session(db)
    b = _new_session(db)
    db.create_prompt_checkpoint(session_id=a, prompt_index=1, messages=[], label="x")
    db.create_prompt_checkpoint(session_id=b, prompt_index=1, messages=[], label="y")
    rows_a = db.list_prompt_checkpoints(a)
    rows_b = db.list_prompt_checkpoints(b)
    assert {r.label for r in rows_a} == {"x"}
    assert {r.label for r in rows_b} == {"y"}


def test_create_prompt_checkpoint_with_files_snapshot():
    """``files_snapshot_json`` is opt-in: caller may pass a dict that
    gets JSON-encoded; round-trip preserves it."""
    db = _fresh_db()
    sid = _new_session(db)
    files = {"/tmp/a.py": "hash1", "/tmp/b.py": "hash2"}
    cp_id = db.create_prompt_checkpoint(
        session_id=sid,
        prompt_index=1,
        messages=[],
        label="with-files",
        files_snapshot=files,
    )
    cp = db.get_prompt_checkpoint(cp_id)
    assert cp is not None
    assert cp.files_snapshot == files


def test_create_prompt_checkpoint_files_snapshot_default_none():
    db = _fresh_db()
    sid = _new_session(db)
    cp_id = db.create_prompt_checkpoint(
        session_id=sid, prompt_index=1, messages=[], label="x"
    )
    cp = db.get_prompt_checkpoint(cp_id)
    assert cp is not None
    assert cp.files_snapshot is None
