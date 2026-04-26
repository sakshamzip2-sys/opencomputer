"""Regression test for the schema column-drift self-heal.

Captures the exact failure mode hit on a real local DB: ``schema_version``
claimed v4 (current ``SCHEMA_VERSION``) but ``messages`` was missing
``reasoning_details`` / ``codex_reasoning_items`` — the v1→v2 ALTER
never fired against this particular DB at some point. Without the
self-heal in :func:`apply_migrations`, the first assistant turn that
tried to persist would crash with ``OperationalError: table messages
has no column named reasoning_details``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from opencomputer.agent.state import (
    SessionDB,
    apply_migrations,
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _drop_column(conn: sqlite3.Connection, table: str, column: str) -> None:
    """Best-effort drop. Silently no-ops if the column was never there."""
    try:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    except sqlite3.OperationalError:
        pass


def test_self_heal_repairs_drift_left_by_partial_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "diverged.db"
    SessionDB(db_path)
    with sqlite3.connect(db_path) as conn:
        _drop_column(conn, "messages", "reasoning_details")
        _drop_column(conn, "messages", "codex_reasoning_items")
        _drop_column(conn, "episodic_events", "dreamed_into")
        conn.commit()
        before = _columns(conn, "messages")
    assert "reasoning_details" not in before, (
        "test setup bug: SQLite refused to drop the column we wanted to simulate "
        "as missing — self-heal can't be tested without that drift"
    )

    SessionDB(db_path)

    with sqlite3.connect(db_path) as conn:
        after_msg = _columns(conn, "messages")
        after_ep = _columns(conn, "episodic_events")

    assert "reasoning_details" in after_msg
    assert "codex_reasoning_items" in after_msg
    assert "dreamed_into" in after_ep


def test_self_heal_is_idempotent_on_fresh_db(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    SessionDB(db_path)
    SessionDB(db_path)
    SessionDB(db_path)

    with sqlite3.connect(db_path) as conn:
        msg_cols = _columns(conn, "messages")
    assert "reasoning_details" in msg_cols
    assert "codex_reasoning_items" in msg_cols


def test_apply_migrations_runs_self_heal_after_migration_loop(tmp_path: Path) -> None:
    db_path = tmp_path / "manual.db"
    SessionDB(db_path)

    with sqlite3.connect(db_path) as conn:
        _drop_column(conn, "messages", "reasoning_details")
        conn.commit()

        apply_migrations(conn)

        cols = _columns(conn, "messages")
    assert "reasoning_details" in cols
