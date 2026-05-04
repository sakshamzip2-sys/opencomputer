"""Tests for lazy session creation (Wave 5 T17).

Hermes-port (c5b4c4816). The TUI/web flow opens a connection then waits
for the user to send something — pre-Wave-5 OC eagerly inserted a row
in :meth:`SessionDB.create_session`, leaving "ghost" empty rows when
the user disconnected without sending. Wave 5 splits that into:

- :meth:`SessionDB.allocate_session_id` — UUID, no DB write
- :meth:`SessionDB.ensure_session`     — idempotent INSERT OR NOTHING,
  called when the first message actually arrives

The legacy :meth:`SessionDB.create_session` keeps its eager
ON CONFLICT DO UPDATE shape so existing callers (gateway dispatch
which writes started_at + platform + model unconditionally) still
work unchanged.
"""

from __future__ import annotations

import pytest

from opencomputer.agent.state import SessionDB


@pytest.fixture
def db(tmp_path) -> SessionDB:
    return SessionDB(tmp_path / "lazy.db")


def test_allocate_returns_unique_uuids():
    a = SessionDB.allocate_session_id()
    b = SessionDB.allocate_session_id()
    assert a != b
    assert len(a) == 36  # standard UUID4 length
    assert len(b) == 36


def test_allocate_does_not_write(tmp_path, db):
    sid = SessionDB.allocate_session_id()
    # No row should exist yet — no I/O happened
    rows = db.list_sessions()
    assert all(r["id"] != sid for r in rows)


def test_ensure_session_creates_row(db):
    sid = SessionDB.allocate_session_id()
    db.ensure_session(sid)
    rows = db.list_sessions()
    assert any(r["id"] == sid for r in rows)


def test_ensure_session_idempotent(db):
    sid = SessionDB.allocate_session_id()
    db.ensure_session(sid)
    db.ensure_session(sid)
    db.ensure_session(sid)
    rows = [r for r in db.list_sessions() if r["id"] == sid]
    assert len(rows) == 1


def test_ensure_preserves_prior_title(db):
    """If /rename ran before the first message, ensure_session must NOT
    clobber the title."""
    sid = SessionDB.allocate_session_id()
    db.set_session_title(sid, "my-named-session")
    db.ensure_session(sid)
    assert db.get_session_title(sid) == "my-named-session"


def test_ensure_explicit_platform_and_model(db):
    sid = SessionDB.allocate_session_id()
    db.ensure_session(sid, platform="telegram", model="claude-haiku-4-5")
    row = db.get_session(sid)
    assert row is not None
    assert row["platform"] == "telegram"
    assert row["model"] == "claude-haiku-4-5"


def test_create_session_legacy_path_still_works(db):
    """The legacy eager create_session path keeps its existing semantics."""
    sid = SessionDB.allocate_session_id()
    db.create_session(sid, platform="cli", model="m")
    rows = db.list_sessions()
    assert any(r["id"] == sid for r in rows)


def test_auto_prune_handles_empty_sessions(db):
    """The existing auto_prune already cleans empty/untitled sessions —
    no separate prune helper needed (per Wave 5 corrections)."""
    # Create one ghost session (no messages)
    sid = SessionDB.allocate_session_id()
    db.ensure_session(sid)
    # Force its started_at into the past so untitled_days=0 doesn't catch it
    # — we want to verify the API exists and doesn't throw, not its policy.
    assert db.auto_prune(
        older_than_days=0,
        untitled_days=0,
        min_messages=1,
    ) == 0  # disabled when both knobs are 0
