"""v18 schema migration + per-session compaction counter helpers.

Source spec: ``docs/superpowers/specs/2026-05-10-cc-usage-context-visibility-design.md``.

Coverage:
    1. Migration v17 → v18 adds ``sessions.compactions_count INTEGER DEFAULT 0``
       and is idempotent.
    2. ``SessionDB.increment_compaction_count(session_id)`` atomically bumps
       the counter and returns the new value.
    3. ``SessionDB.session_usage_summary(session_id)`` returns a frozen
       dataclass row with token + cache + compaction totals, joined to
       per-call cost from ``llm_calls``.
    4. ``SessionDB.usage_summary_aggregate(...)`` returns per-session rows
       filterable by ``since`` / ``model`` / ``provider``.
    5. Adversarial inputs (unknown session, NULL cache columns, malformed
       DB) do not raise — they return safe defaults.
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from opencomputer.agent.state import (
    DDL,
    SCHEMA_VERSION,
    SessionDB,
    SessionUsageRow,
    _read_schema_version,
    apply_migrations,
)


def _fresh_conn() -> sqlite3.Connection:
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    conn = sqlite3.connect(tmp)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _fresh_db() -> SessionDB:
    return SessionDB(Path(tempfile.mkdtemp()) / "t.db")


def _new_session(db: SessionDB, *, model: str = "", started_at: float | None = None) -> str:
    sid = db.allocate_session_id()
    db.create_session(sid, platform="cli", model=model)
    if started_at is not None:
        with db._connect() as conn:
            conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (started_at, sid))
    return sid


# ---------------------------------------------------------------------------
# 1. Migration
# ---------------------------------------------------------------------------


def test_schema_version_is_at_least_18():
    """v18 = compactions_count column on sessions for /context surfacing."""
    assert SCHEMA_VERSION >= 18


def test_v17_to_v18_adds_compactions_count_column():
    conn = _fresh_conn()
    # simulate a v17 DB: apply baseline DDL minus v18, then bump version manually
    conn.executescript(DDL)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "compactions_count" in cols:
        # Baseline DDL already carries v18 shape (fresh-DB path); rebuild without
        # the column to simulate a true v17 DB.
        try:
            conn.execute("ALTER TABLE sessions DROP COLUMN compactions_count")
        except sqlite3.OperationalError:
            pytest.skip("sqlite without DROP COLUMN; idempotency test covers this")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (17)")
    conn.commit()
    apply_migrations(conn)

    cols2 = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "compactions_count" in cols2
    assert _read_schema_version(conn) == SCHEMA_VERSION


def test_v17_to_v18_is_idempotent():
    conn = _fresh_conn()
    apply_migrations(conn)
    apply_migrations(conn)
    apply_migrations(conn)
    assert _read_schema_version(conn) == SCHEMA_VERSION
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "compactions_count" in cols


def test_v17_to_v18_legacy_data_preserved():
    """Rows inserted at v17 read with compactions_count=0 after migration."""
    conn = _fresh_conn()
    conn.executescript(DDL)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "compactions_count" in cols:
        try:
            conn.execute("ALTER TABLE sessions DROP COLUMN compactions_count")
        except sqlite3.OperationalError:
            pytest.skip("sqlite without DROP COLUMN")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (17)")
    conn.execute(
        "INSERT INTO sessions(id, started_at, platform) VALUES ('legacy', 100.0, 'cli')"
    )
    conn.commit()

    apply_migrations(conn)

    row = conn.execute(
        "SELECT id, compactions_count FROM sessions WHERE id='legacy'"
    ).fetchone()
    assert row == ("legacy", 0)


# ---------------------------------------------------------------------------
# 2. increment_compaction_count
# ---------------------------------------------------------------------------


def test_increment_compaction_count_starts_at_zero_then_bumps():
    db = _fresh_db()
    sid = _new_session(db, model="claude-opus-4-7")

    n1 = db.increment_compaction_count(sid)
    assert n1 == 1
    n2 = db.increment_compaction_count(sid)
    assert n2 == 2
    n3 = db.increment_compaction_count(sid)
    assert n3 == 3

    # Verify persistence on a fresh connection.
    with db._connect() as conn:
        row = conn.execute(
            "SELECT compactions_count FROM sessions WHERE id=?", (sid,)
        ).fetchone()
    assert row[0] == 3


def test_increment_compaction_count_unknown_session_returns_zero():
    """Unknown session id: helper logs a warning but returns 0 — never raises."""
    db = _fresh_db()
    n = db.increment_compaction_count("does-not-exist")
    assert n == 0


def test_increment_compaction_count_rejects_empty_or_none_session_id():
    """Adversarial input: empty / None session id is treated as unknown,
    returning 0 (validation pattern matches existing add_tokens)."""
    db = _fresh_db()
    assert db.increment_compaction_count("") == 0
    assert db.increment_compaction_count(None) == 0  # type: ignore[arg-type]


def test_increment_compaction_count_two_sessions_independent():
    db = _fresh_db()
    a = _new_session(db, model="m1")
    b = _new_session(db, model="m2")
    db.increment_compaction_count(a)
    db.increment_compaction_count(a)
    db.increment_compaction_count(b)

    with db._connect() as conn:
        row_a = conn.execute("SELECT compactions_count FROM sessions WHERE id=?", (a,)).fetchone()
        row_b = conn.execute("SELECT compactions_count FROM sessions WHERE id=?", (b,)).fetchone()
    assert row_a[0] == 2
    assert row_b[0] == 1


# ---------------------------------------------------------------------------
# 3. session_usage_summary
# ---------------------------------------------------------------------------


def test_session_usage_summary_returns_row_for_known_session():
    db = _fresh_db()
    sid = _new_session(db, model="claude-opus-4-7")
    db.add_tokens(
        session_id=sid,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=30,
        cache_write_tokens=20,
    )
    db.increment_compaction_count(sid)

    row = db.session_usage_summary(sid)
    assert row is not None
    assert isinstance(row, SessionUsageRow)
    assert row.session_id == sid
    assert row.input_tokens == 100
    assert row.output_tokens == 50
    assert row.cache_read_tokens == 30
    assert row.cache_write_tokens == 20
    assert row.compactions_count == 1
    assert row.model == "claude-opus-4-7"


def test_session_usage_summary_returns_none_for_unknown_session():
    db = _fresh_db()
    assert db.session_usage_summary("does-not-exist") is None


def test_session_usage_summary_returns_none_for_empty_id():
    db = _fresh_db()
    assert db.session_usage_summary("") is None


def test_session_usage_summary_joins_llm_calls_for_cost():
    db = _fresh_db()
    sid = _new_session(db, model="claude-opus-4-7")
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO llm_calls(session_id, ts, provider, model, input_tokens, output_tokens, cost_usd, batch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, time.time(), "anthropic", "claude-opus-4-7", 100, 50, 0.123, 0),
        )
        conn.execute(
            "INSERT INTO llm_calls(session_id, ts, provider, model, input_tokens, output_tokens, cost_usd, batch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, time.time(), "anthropic", "claude-opus-4-7", 100, 50, 0.456, 0),
        )

    row = db.session_usage_summary(sid)
    assert row is not None
    assert row.cost_usd == pytest.approx(0.579)


def test_session_usage_summary_cost_is_none_when_no_llm_calls():
    db = _fresh_db()
    sid = _new_session(db, model="claude-opus-4-7")
    row = db.session_usage_summary(sid)
    assert row is not None
    assert row.cost_usd is None


def test_session_usage_summary_handles_null_cost_calls():
    """llm_calls.cost_usd is nullable (some models lack pricing). SUM
    over only NULL costs returns NULL and we surface that as cost_usd=None."""
    db = _fresh_db()
    sid = _new_session(db, model="some-unpriced-model")
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO llm_calls(session_id, ts, provider, model, input_tokens, output_tokens, cost_usd, batch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, time.time(), "x", "some-unpriced-model", 10, 5, None, 0),
        )
    row = db.session_usage_summary(sid)
    assert row is not None
    assert row.cost_usd is None


# ---------------------------------------------------------------------------
# 4. usage_summary_aggregate
# ---------------------------------------------------------------------------


def test_usage_summary_aggregate_returns_per_session_rows():
    db = _fresh_db()
    a = _new_session(db, model="opus")
    b = _new_session(db, model="sonnet")
    db.add_tokens(session_id=a, input_tokens=100, output_tokens=50)
    db.add_tokens(session_id=b, input_tokens=200, output_tokens=100)

    rows = db.usage_summary_aggregate()
    assert len(rows) == 2
    by_id = {r.session_id: r for r in rows}
    assert by_id[a].input_tokens == 100
    assert by_id[b].input_tokens == 200


def test_usage_summary_aggregate_filters_by_model():
    db = _fresh_db()
    a = _new_session(db, model="opus-4-7")
    b = _new_session(db, model="sonnet-4-6")
    db.add_tokens(session_id=a, input_tokens=100, output_tokens=50)
    db.add_tokens(session_id=b, input_tokens=200, output_tokens=100)

    rows = db.usage_summary_aggregate(model="opus-4-7")
    assert len(rows) == 1
    assert rows[0].session_id == a


def test_usage_summary_aggregate_filters_by_since():
    db = _fresh_db()
    old = _new_session(db, model="m", started_at=1000.0)
    new = _new_session(db, model="m", started_at=3000.0)
    db.add_tokens(session_id=old, input_tokens=10, output_tokens=5)
    db.add_tokens(session_id=new, input_tokens=20, output_tokens=10)

    rows = db.usage_summary_aggregate(since=2000.0)
    assert len(rows) == 1
    assert rows[0].session_id == new


def test_usage_summary_aggregate_limit_applied_after_order():
    db = _fresh_db()
    ids = []
    for i in range(5):
        sid = _new_session(db, model="m", started_at=1000.0 + i)
        ids.append(sid)
    rows = db.usage_summary_aggregate(limit=3)
    assert len(rows) == 3
    # Most recent first
    assert rows[0].session_id == ids[-1]
    assert rows[1].session_id == ids[-2]
    assert rows[2].session_id == ids[-3]


def test_usage_summary_aggregate_filters_by_provider():
    db = _fresh_db()
    a = _new_session(db, model="opus")
    b = _new_session(db, model="gpt-4")
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO llm_calls(session_id, ts, provider, model, input_tokens, output_tokens, cost_usd, batch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (a, time.time(), "anthropic", "opus", 10, 5, 0.01, 0),
        )
        conn.execute(
            "INSERT INTO llm_calls(session_id, ts, provider, model, input_tokens, output_tokens, cost_usd, batch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (b, time.time(), "openai", "gpt-4", 10, 5, 0.01, 0),
        )

    rows = db.usage_summary_aggregate(provider="anthropic")
    assert len(rows) == 1
    assert rows[0].session_id == a


def test_usage_summary_aggregate_limit_validation():
    """Adversarial: negative or zero limit clamps to 1."""
    db = _fresh_db()
    _new_session(db, model="m")
    rows1 = db.usage_summary_aggregate(limit=0)
    rows2 = db.usage_summary_aggregate(limit=-5)
    assert len(rows1) == 1
    assert len(rows2) == 1


def test_usage_summary_aggregate_empty_db_returns_empty_list():
    db = _fresh_db()
    rows = db.usage_summary_aggregate()
    assert rows == []
