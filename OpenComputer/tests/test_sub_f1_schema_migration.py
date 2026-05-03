"""Schema migration framework: v1 baseline → v2 (II.6 reasoning columns) → v3 (F1 consent tables)."""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from opencomputer.agent.state import (
    DDL,
    SCHEMA_VERSION,
    _read_schema_version,
    apply_migrations,
)


def _fresh_conn() -> sqlite3.Connection:
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    conn = sqlite3.connect(tmp)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_schema_version_is_7():
    # v1 = baseline; v2 = II.6 reasoning-chain metadata columns on ``messages``;
    # v3 = F1 consent tables; v4 = P-18 episodic dreamed_into column;
    # v5 = Tier-A item 11 tool_usage table; v6 = per-turn vibe_log;
    # v7 = Phase 0 outcome-aware learning (turn_outcomes + recall_citations).
    assert SCHEMA_VERSION == 7


def test_apply_migrations_on_fresh_db_creates_all_tables():
    conn = _fresh_conn()
    apply_migrations(conn)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    # existing v1 tables
    assert "sessions" in tables
    assert "messages" in tables
    assert "episodic_events" in tables
    # F1 consent-layer tables (added in v3)
    assert "consent_grants" in tables
    assert "consent_counters" in tables
    assert "audit_log" in tables


def test_apply_migrations_is_idempotent():
    conn = _fresh_conn()
    apply_migrations(conn)
    apply_migrations(conn)  # second call should be a no-op
    assert _read_schema_version(conn) == 7


def test_existing_v1_db_migrates_with_data_preserved():
    conn = _fresh_conn()
    # simulate a v1 DB (apply only the original v1 DDL, set version to 1)
    conn.executescript(DDL)
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (1)")
    conn.execute(
        "INSERT INTO sessions(id, started_at, platform) VALUES (?, ?, ?)",
        ("s1", 100.0, "cli"),
    )
    conn.commit()

    apply_migrations(conn)
    assert _read_schema_version(conn) == 7
    got = conn.execute("SELECT id FROM sessions WHERE id='s1'").fetchone()
    assert got == ("s1",)  # data preserved


def test_audit_log_blocks_update_and_delete():
    conn = _fresh_conn()
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO audit_log(session_id, timestamp, actor, action, "
        "capability_id, tier, scope, decision, reason, prev_hmac, row_hmac) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("s1", 100.0, "user", "grant", "read_files", 1, None, "allow",
         "", "0" * 64, "ab" * 32),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET reason='tampered'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log")


def test_read_schema_version_returns_zero_on_fresh_db():
    conn = _fresh_conn()
    assert _read_schema_version(conn) == 0


def test_consent_grants_has_expected_columns():
    conn = _fresh_conn()
    apply_migrations(conn)
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(consent_grants)"
    ).fetchall()}
    assert cols == {
        "capability_id", "scope_filter", "tier",
        "granted_at", "expires_at", "granted_by",
    }


def test_audit_log_has_hmac_columns():
    conn = _fresh_conn()
    apply_migrations(conn)
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(audit_log)"
    ).fetchall()}
    assert "prev_hmac" in cols
    assert "row_hmac" in cols
    assert "capability_id" in cols
    assert "actor" in cols
