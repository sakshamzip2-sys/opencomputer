"""Tests for sessions.vacuum_after_prune — VACUUM after auto-prune."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest


def test_default_vacuum_after_prune_is_true() -> None:
    """Hermes spec says default is true."""
    from opencomputer.agent.config import default_config

    cfg = default_config()
    assert cfg.session.vacuum_after_prune is True


def test_load_config_parses_vacuum_after_prune(tmp_path: Path) -> None:
    from opencomputer.agent.config_store import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "session:\n  vacuum_after_prune: false\n", encoding="utf-8"
    )
    cfg = load_config(cfg_path)
    assert cfg.session.vacuum_after_prune is False


def _stuff_old_sessions(db_path: Path, n: int) -> None:
    """Populate the SessionDB with ``n`` old sessions for prune to delete."""
    cutoff = time.time() - 365 * 86400  # 1 year old
    with sqlite3.connect(db_path) as conn:
        for i in range(n):
            sid = f"old-{i:04d}"
            conn.execute(
                "INSERT INTO sessions "
                "(id, started_at, platform, title, message_count) "
                "VALUES (?, ?, 'cli', NULL, 5)",
                (sid, cutoff),
            )
            # Pad with messages so DELETE has rows to prune from messages too.
            for _j in range(5):
                conn.execute(
                    "INSERT INTO messages "
                    "(session_id, role, content, timestamp) "
                    "VALUES (?, 'user', ?, ?)",
                    (sid, "x" * 1000, cutoff),
                )
        conn.commit()


def _total_db_size(db_path: Path) -> int:
    """Sum main DB file + WAL + SHM (SQLite WAL splits storage)."""
    total = 0
    for name in (db_path.name, db_path.name + "-wal", db_path.name + "-shm"):
        p = db_path.parent / name
        if p.exists():
            total += p.stat().st_size
    return total


def _freelist_count(db_path: Path) -> int:
    """Return SQLite freelist page count (free pages waiting for VACUUM)."""
    with sqlite3.connect(db_path) as conn:
        return conn.execute("PRAGMA freelist_count").fetchone()[0]


def test_vacuum_called_when_enabled(tmp_path: Path) -> None:
    """vacuum_after_prune=True clears the freelist (VACUUM ran)."""
    from opencomputer.agent.state import SessionDB

    db_path = tmp_path / "state.db"
    SessionDB(db_path)
    _stuff_old_sessions(db_path, 50)
    db = SessionDB(db_path)
    deleted = db.auto_prune(
        older_than_days=30,
        untitled_days=0,
        min_messages=3,
        vacuum_after_prune=True,
    )
    assert deleted > 0
    # Force WAL checkpoint so the on-disk PRAGMA reflects post-VACUUM state.
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # After VACUUM, freelist is empty (no orphaned free pages).
    assert _freelist_count(db_path) == 0


def test_vacuum_skipped_when_disabled(tmp_path: Path) -> None:
    """vacuum_after_prune=False leaves a non-zero freelist (no VACUUM ran)."""
    from opencomputer.agent.state import SessionDB

    db_path = tmp_path / "state.db"
    SessionDB(db_path)
    _stuff_old_sessions(db_path, 50)
    db = SessionDB(db_path)
    deleted = db.auto_prune(
        older_than_days=30,
        untitled_days=0,
        min_messages=3,
        vacuum_after_prune=False,
    )
    assert deleted > 0
    # Force checkpoint so pragma reflects committed state.
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # Without VACUUM, the freed pages remain in the freelist.
    assert _freelist_count(db_path) > 0


def test_vacuum_skipped_when_no_rows_deleted(tmp_path: Path) -> None:
    """No-op when policies wouldn't match anything: VACUUM not run, returns 0."""
    from opencomputer.agent.state import SessionDB

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    deleted = db.auto_prune(
        older_than_days=30,
        untitled_days=0,
        min_messages=3,
        vacuum_after_prune=True,
    )
    assert deleted == 0
