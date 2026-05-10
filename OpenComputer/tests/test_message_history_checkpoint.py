"""M5.2 — per-prompt message-history checkpoints (CheckpointManager).

Pins the contract added 2026-05-09:

* Schema v15 introduces ``prompt_checkpoints`` table.
* :class:`CheckpointManager` create/list/restore_messages/restore_files.
* Migration from v14 adds the table without losing data.
* Loop wiring (see test_loop_creates_checkpoint_before_tool_use).
* Checkpoint id is content-stable (same input → same id when no
  collision) and per-session (different sessions don't collide).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from opencomputer.agent.checkpoint_manager import (
    CheckpointManager,
    MessageCheckpoint,
    _checkpoint_id,
)
from opencomputer.agent.state import SCHEMA_VERSION, SessionDB


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "sessions.db")


@pytest.fixture
def session(db: SessionDB) -> str:
    """Create a session row so FK constraints are satisfied."""
    sid = "sess-test-12345678"
    db.create_session(
        session_id=sid,
        platform="cli",
        model="test-model",
    )
    return sid


# ─── schema migration ────────────────────────────────────────────────────


class TestSchema:
    def test_schema_version_is_at_or_above_15(self) -> None:
        # Floor assertion (was hardcoded 15; bumped to 16 by
        # delegate-lineage 2026-05-10 — `parent_session_id` column +
        # `subagents` table). Future migrations should keep this floor
        # rather than re-baking exact equality.
        assert SCHEMA_VERSION >= 15

    def test_fresh_db_has_prompt_checkpoints_table(self, db: SessionDB) -> None:
        with db._connect() as conn:
            row = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='prompt_checkpoints'
                """
            ).fetchone()
        assert row is not None

    def test_table_has_expected_columns(self, db: SessionDB) -> None:
        with db._connect() as conn:
            cols = {
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(prompt_checkpoints)"
                ).fetchall()
            }
        assert {
            "id",
            "session_id",
            "prompt_index",
            "messages_snapshot_json",
            "files_snapshot_json",
            "label",
            "created_at",
        } <= cols

    def test_index_on_session_id_exists(self, db: SessionDB) -> None:
        with db._connect() as conn:
            indexes = {
                r[0]
                for r in conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='index' AND tbl_name='prompt_checkpoints'
                    """
                ).fetchall()
            }
        assert "idx_prompt_checkpoints_session" in indexes


# ─── migration from v14 ──────────────────────────────────────────────────


class TestMigrationFromV14:
    def test_v14_db_gets_prompt_checkpoints_table(self, tmp_path: Path) -> None:
        # Build a v14-shaped DB by hand
        db_path = tmp_path / "legacy.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "CREATE TABLE schema_version (version INTEGER NOT NULL)"
            )
            conn.execute("INSERT INTO schema_version (version) VALUES (14)")
            conn.execute(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    platform TEXT NOT NULL
                )
                """
            )
            conn.commit()

        # Open through SessionDB — apply_migrations runs and upgrades
        # past v15 (current floor; delegate-lineage bumped to v16).
        db = SessionDB(db_path)
        with db._connect() as conn:
            row = conn.execute(
                "SELECT version FROM schema_version"
            ).fetchone()
            assert int(row[0]) >= 15

            row = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='prompt_checkpoints'
                """
            ).fetchone()
            assert row is not None


# ─── _checkpoint_id stability ────────────────────────────────────────────


class TestCheckpointIdStability:
    def test_same_inputs_yield_same_id(self) -> None:
        a = _checkpoint_id("sess-1", 0, 1234567890.0)
        b = _checkpoint_id("sess-1", 0, 1234567890.0)
        assert a == b

    def test_different_session_yields_different_id(self) -> None:
        a = _checkpoint_id("sess-1", 0, 1234567890.0)
        b = _checkpoint_id("sess-2", 0, 1234567890.0)
        assert a != b

    def test_different_index_yields_different_id(self) -> None:
        a = _checkpoint_id("sess-1", 0, 1234567890.0)
        b = _checkpoint_id("sess-1", 1, 1234567890.0)
        assert a != b


# ─── CheckpointManager.create ────────────────────────────────────────────


class TestCreate:
    def test_create_persists_checkpoint(
        self, db: SessionDB, session: str
    ) -> None:
        mgr = CheckpointManager(db)
        msgs = [{"role": "user", "content": "hi"}]
        cp = mgr.create(session_id=session, messages=msgs)

        assert isinstance(cp, MessageCheckpoint)
        assert cp.session_id == session
        assert cp.prompt_index == 0
        assert cp.label.startswith("before tool_use")
        assert cp.files_snapshot_json is None  # opt-in

    def test_create_increments_prompt_index(
        self, db: SessionDB, session: str
    ) -> None:
        mgr = CheckpointManager(db)
        cp1 = mgr.create(session_id=session, messages=[])
        cp2 = mgr.create(session_id=session, messages=[])
        cp3 = mgr.create(session_id=session, messages=[])
        assert [cp1.prompt_index, cp2.prompt_index, cp3.prompt_index] == [0, 1, 2]

    def test_create_with_files_snapshot(
        self, db: SessionDB, session: str
    ) -> None:
        mgr = CheckpointManager(db)
        files = {"src/foo.py": b"print('hi')\n", "README.md": b"# Hello\n"}
        cp = mgr.create(session_id=session, messages=[], files=files)

        assert cp.files_snapshot_json is not None
        # Round-trip via restore_files
        restored = mgr.restore_files(cp.id)
        assert restored == files

    def test_create_with_custom_label(
        self, db: SessionDB, session: str
    ) -> None:
        mgr = CheckpointManager(db)
        cp = mgr.create(
            session_id=session,
            messages=[],
            label="manual-pin-before-rebase",
        )
        assert cp.label == "manual-pin-before-rebase"


# ─── CheckpointManager.list ──────────────────────────────────────────────


class TestList:
    def test_list_empty(self, db: SessionDB, session: str) -> None:
        mgr = CheckpointManager(db)
        assert mgr.list(session) == []

    def test_list_newest_first(
        self, db: SessionDB, session: str
    ) -> None:
        import time

        mgr = CheckpointManager(db)
        cp1 = mgr.create(session_id=session, messages=[])
        time.sleep(0.01)  # ensure created_at differs
        cp2 = mgr.create(session_id=session, messages=[])
        time.sleep(0.01)
        cp3 = mgr.create(session_id=session, messages=[])

        listed = mgr.list(session)
        assert [c.id for c in listed] == [cp3.id, cp2.id, cp1.id]

    def test_list_filters_by_session(
        self, db: SessionDB, session: str
    ) -> None:
        mgr = CheckpointManager(db)
        # Create a second session row for the FK
        other_sid = "sess-other-87654321"
        db.create_session(
            session_id=other_sid, platform="cli", model="test-model"
        )

        cp_a = mgr.create(session_id=session, messages=[])
        cp_b = mgr.create(session_id=other_sid, messages=[])

        a_list = mgr.list(session)
        b_list = mgr.list(other_sid)
        assert [c.id for c in a_list] == [cp_a.id]
        assert [c.id for c in b_list] == [cp_b.id]

    def test_list_respects_limit(self, db: SessionDB, session: str) -> None:
        mgr = CheckpointManager(db)
        for _ in range(5):
            mgr.create(session_id=session, messages=[])
        assert len(mgr.list(session, limit=3)) == 3


# ─── CheckpointManager.restore_messages ──────────────────────────────────


class TestRestoreMessages:
    def test_restore_returns_decoded_messages(
        self, db: SessionDB, session: str
    ) -> None:
        mgr = CheckpointManager(db)
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        cp = mgr.create(session_id=session, messages=msgs)
        restored = mgr.restore_messages(cp.id)
        assert restored == msgs

    def test_restore_unknown_returns_none(self, db: SessionDB) -> None:
        mgr = CheckpointManager(db)
        assert mgr.restore_messages("nonexistent-id") is None

    def test_messages_method_round_trips(
        self, db: SessionDB, session: str
    ) -> None:
        mgr = CheckpointManager(db)
        msgs = [{"role": "user", "content": "x"}]
        cp = mgr.create(session_id=session, messages=msgs)
        # Reload via list then call .messages()
        listed = mgr.list(session)[0]
        assert listed.messages() == msgs


# ─── files snapshot opt-in ───────────────────────────────────────────────


class TestRestoreFiles:
    def test_restore_files_when_no_snapshot_returns_none(
        self, db: SessionDB, session: str
    ) -> None:
        mgr = CheckpointManager(db)
        cp = mgr.create(session_id=session, messages=[])
        assert mgr.restore_files(cp.id) is None

    def test_restore_files_round_trips_binary_safely(
        self, db: SessionDB, session: str
    ) -> None:
        mgr = CheckpointManager(db)
        # Bytes including null + high-bit chars (latin-1 encoding ensures
        # round-trip through JSON without UTF-8 decode errors).
        files = {"binary.bin": b"\x00\xff\x80\x7f\xfe"}
        cp = mgr.create(session_id=session, messages=[], files=files)
        assert mgr.restore_files(cp.id) == files
