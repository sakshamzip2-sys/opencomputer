"""Schema migration v20 → v21 — the ``gateway_parity_log`` telemetry table.

M1/T1.2 of the gateway-vs-CLI intelligence-parity plan
(``docs/superpowers/specs/2026-05-17-gateway-vs-cli-parity/PLAN.md``).

The table records, per gateway turn, which of the 10 parity-affecting
mechanisms fired. It is operational telemetry — a plain append table,
no HMAC chain, no append-only trigger (mirrors v20 ``tool_loop_trips``).
"""
import sqlite3
import tempfile
from pathlib import Path

from opencomputer.agent.state import (
    SCHEMA_VERSION,
    _read_schema_version,
    apply_migrations,
)


def _fresh_conn() -> sqlite3.Connection:
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    return sqlite3.connect(tmp)


def test_schema_version_at_least_21() -> None:
    assert SCHEMA_VERSION >= 21


def test_fresh_db_has_gateway_parity_log_table() -> None:
    conn = _fresh_conn()
    apply_migrations(conn)
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "gateway_parity_log" in tables


def test_gateway_parity_log_columns() -> None:
    conn = _fresh_conn()
    apply_migrations(conn)
    cols = {
        row[1]: row[2]
        for row in conn.execute("PRAGMA table_info(gateway_parity_log)")
    }
    assert cols == {
        "id": "INTEGER",
        "ts": "REAL",
        "session_id": "TEXT",
        "turn_id": "INTEGER",
        "platform": "TEXT",
        "mechanism_id": "TEXT",
        "fired": "INTEGER",
        "detail": "TEXT",
    }


def test_gateway_parity_log_indexes_exist() -> None:
    conn = _fresh_conn()
    apply_migrations(conn)
    indexes = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_gateway_parity_log_session" in indexes
    assert "idx_gateway_parity_log_mechanism" in indexes


def test_migration_is_idempotent() -> None:
    conn = _fresh_conn()
    apply_migrations(conn)
    # Re-running must not raise and must leave the version unchanged.
    apply_migrations(conn)
    assert _read_schema_version(conn) == SCHEMA_VERSION


def test_legacy_v20_db_self_heals_to_v21() -> None:
    """A DB stuck at v20 (no parity table) gains it on next apply."""
    conn = _fresh_conn()
    # Simulate a v20 DB: run migrations, then force the version back.
    apply_migrations(conn)
    conn.execute("DROP TABLE gateway_parity_log")
    conn.execute("UPDATE schema_version SET version = 20")
    conn.commit()
    apply_migrations(conn)
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "gateway_parity_log" in tables
    assert _read_schema_version(conn) == SCHEMA_VERSION


def test_gateway_parity_log_accepts_a_row() -> None:
    conn = _fresh_conn()
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO gateway_parity_log "
        "(ts, session_id, turn_id, platform, mechanism_id, fired, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1.0, "sess", 3, "telegram", "prompt_override", 1, "{}"),
    )
    conn.commit()
    row = conn.execute(
        "SELECT session_id, mechanism_id, fired FROM gateway_parity_log"
    ).fetchone()
    assert row == ("sess", "prompt_override", 1)
