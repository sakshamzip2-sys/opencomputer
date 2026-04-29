"""Regression test for /rename before first turn — see fix/rename-before-first-turn.

Bug: ``set_session_title`` used a bare UPDATE, so renaming a session
*before* its first message (and thus before ``create_session`` had
inserted the row) silently no-op'd. The slash handler's success message
lied. Then ``create_session`` would have wiped any pre-set title anyway
because it used ``INSERT OR REPLACE``.

Fix: ``set_session_title`` is now an UPSERT that creates a minimal row
if missing; ``create_session`` is now an UPSERT that preserves any
existing title. Together: rename works regardless of order.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.state import SessionDB


def test_set_title_before_create_session_persists(tmp_path: Path):
    """The user's bug: /rename test-1 fires BEFORE the first message →
    create_session hadn't run → bare UPDATE was a no-op.
    """
    db = SessionDB(tmp_path / "s.db")
    sid = "abc-123"
    # No create_session call — mirrors /rename pre-first-turn.
    db.set_session_title(sid, "test-1")
    assert db.get_session_title(sid) == "test-1"


def test_create_session_preserves_pre_set_title(tmp_path: Path):
    """After /rename pre-creates the row, the first message triggers
    create_session. The title must NOT get wiped."""
    db = SessionDB(tmp_path / "s.db")
    sid = "abc-123"
    db.set_session_title(sid, "test-1")
    db.create_session(sid, platform="cli", model="claude-opus-4-7")
    assert db.get_session_title(sid) == "test-1"


def test_create_session_then_rename_still_works(tmp_path: Path):
    """The other ordering: row exists from a normal first turn, then
    /rename. Should still work (this case worked before the fix; we keep
    it green to prove no regression)."""
    db = SessionDB(tmp_path / "s.db")
    sid = "abc-123"
    db.create_session(sid, platform="cli", model="claude-opus-4-7")
    db.set_session_title(sid, "after-first-turn")
    assert db.get_session_title(sid) == "after-first-turn"


def test_create_session_idempotent_metadata_refresh(tmp_path: Path):
    """Calling create_session twice updates platform/model but leaves
    title alone. (Even though the loop guards with ``if existing is
    None`` today, future refactors may always-call it; the contract
    should be safe.)"""
    db = SessionDB(tmp_path / "s.db")
    sid = "abc-123"
    db.create_session(sid, platform="cli", model="claude-opus-4-7")
    db.set_session_title(sid, "my-title")
    db.create_session(sid, platform="cli", model="claude-sonnet-4-6")
    assert db.get_session_title(sid) == "my-title"


def test_rename_then_overwrite_with_new_title(tmp_path: Path):
    """User renames, then renames again — second value wins."""
    db = SessionDB(tmp_path / "s.db")
    sid = "abc-123"
    db.set_session_title(sid, "first")
    db.set_session_title(sid, "second")
    assert db.get_session_title(sid) == "second"


def test_set_title_creates_row_with_started_at(tmp_path: Path):
    """The auto-created row must have a real started_at so it's a valid
    session entry for downstream listing/resume."""
    import sqlite3

    db = SessionDB(tmp_path / "s.db")
    sid = "abc-123"
    db.set_session_title(sid, "hi")
    conn = sqlite3.connect(tmp_path / "s.db")
    row = conn.execute(
        "SELECT started_at, title FROM sessions WHERE id = ?", (sid,)
    ).fetchone()
    conn.close()
    assert row is not None
    started_at, title = row
    assert title == "hi"
    assert started_at > 0  # a real epoch, not NULL or zero
