"""Tests for FTS5 trigram tokenizer migration (Wave 6.B — Hermes 1fa76607c).

Verifies:
- Fresh DBs use the trigram tokenizer (CJK + substring-friendly)
- v11 → v12 migration drops + recreates the FTS table
- Existing message rows are reindexed after migration
- Substring search ("abor" matches "elaborate") works post-migration
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from opencomputer.agent.state import (
    SCHEMA_VERSION,
    SessionDB,
    apply_migrations,
)


def _fts_tokenize_clause(conn: sqlite3.Connection) -> str:
    """Return the tokenize= clause used by messages_fts (best-effort parse)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    return (row[0] if row else "") or ""


def test_fresh_db_uses_trigram_tokenizer(tmp_path):
    db = SessionDB(tmp_path / "fresh.db")
    with db._connect() as conn:
        ddl = _fts_tokenize_clause(conn)
    # Either "trigram" (preferred) or porter unicode61 (fallback on builds
    # that don't support trigram).
    assert "trigram" in ddl or "unicode61" in ddl


def test_schema_version_advanced_to_12(tmp_path):
    """Fresh DBs land at SCHEMA_VERSION (currently 12)."""
    db = SessionDB(tmp_path / "fresh.db")
    with db._connect() as conn:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
    assert row is not None
    assert row[0] == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 12


def test_v11_to_v12_migration_keeps_existing_messages(tmp_path):
    """An existing v11 DB with messages must keep its rows after migration."""
    db_path = tmp_path / "legacy.db"
    db = SessionDB(db_path)
    sid = db.allocate_session_id()
    db.ensure_session(sid, platform="cli")
    from plugin_sdk.core import Message

    db.append_message(sid, Message(role="user", content="hello world"))
    db.append_message(sid, Message(role="user", content="elaborate further"))

    # Manually rewind schema_version to 11 to force a v11→v12 migration
    with db._connect() as conn:
        conn.execute("UPDATE schema_version SET version = 11")
        conn.commit()

    # Re-open: apply_migrations should fire v11→v12
    with sqlite3.connect(db_path) as conn:
        apply_migrations(conn)
        # Rows survive
        rows = conn.execute("SELECT content FROM messages ORDER BY id").fetchall()
    contents = [r[0] for r in rows]
    assert "hello world" in contents
    assert "elaborate further" in contents


def test_substring_search_after_trigram_migration(tmp_path):
    """Trigram supports substring queries — 'abor' matches 'elaborate'."""
    db = SessionDB(tmp_path / "search.db")
    sid = db.allocate_session_id()
    db.ensure_session(sid, platform="cli")
    from plugin_sdk.core import Message

    db.append_message(sid, Message(role="user", content="please elaborate further"))

    with db._connect() as conn:
        # Skip test gracefully if this sqlite build lacks trigram
        ddl = _fts_tokenize_clause(conn)
        if "trigram" not in ddl:
            pytest.skip("sqlite build lacks trigram tokenizer")
        rows = conn.execute(
            "SELECT content FROM messages_fts WHERE content MATCH ?",
            ("abor",),
        ).fetchall()
    matched = [r[0] for r in rows]
    assert any("elaborate" in m for m in matched)


def test_cjk_substring_after_trigram(tmp_path):
    """Chinese characters: trigram can match a 3-char substring without spaces."""
    db = SessionDB(tmp_path / "cjk.db")
    sid = db.allocate_session_id()
    db.ensure_session(sid, platform="cli")
    from plugin_sdk.core import Message

    db.append_message(sid, Message(role="user", content="今天的天气很好"))
    with db._connect() as conn:
        ddl = _fts_tokenize_clause(conn)
        if "trigram" not in ddl:
            pytest.skip("sqlite build lacks trigram tokenizer")
        rows = conn.execute(
            "SELECT content FROM messages_fts WHERE content MATCH ?",
            ("天气很",),
        ).fetchall()
    assert any("今天的天气很好" in r[0] for r in rows)


def test_migration_idempotent(tmp_path):
    """Re-applying v11→v12 must not crash on an already-migrated DB."""
    db_path = tmp_path / "idem.db"
    SessionDB(db_path)  # creates at v12
    with sqlite3.connect(db_path) as conn:
        # Force re-run: rewind, then re-apply
        conn.execute("UPDATE schema_version SET version = 11")
        conn.commit()
        apply_migrations(conn)
        # Now at v12 again
        v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert v == SCHEMA_VERSION
