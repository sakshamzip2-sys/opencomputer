"""Tests for opencomputer.evolution.storage — migration runner and CRUD API.

All tests use in-memory or tmp_path SQLite databases.  No interaction with
the real ~/.opencomputer profile.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from opencomputer.evolution.storage import (
    apply_pending,
    count_records,
    evolution_home,
    get_record,
    init_db,
    insert_record,
    list_recent,
    purge_older_than,
    trajectory_db_path,
    update_reward,
)
from opencomputer.evolution.trajectory import (
    SCHEMA_VERSION_CURRENT,
    TrajectoryEvent,
    TrajectoryRecord,
    new_event,
    new_record,
    with_event,
)

# ---------------------------------------------------------------------------
# Shared in-memory fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Open an in-memory SQLite connection with migrations applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _make_event(session_id: str = "sess-1", **kwargs) -> TrajectoryEvent:
    defaults = dict(
        session_id=session_id,
        message_id=None,
        action_type="tool_call",
        tool_name="Bash",
        outcome="success",
        timestamp=time.time(),
        metadata={},
    )
    defaults.update(kwargs)
    return TrajectoryEvent(**defaults)


def _make_record(
    session_id: str = "sess-1",
    events: tuple[TrajectoryEvent, ...] = (),
    started_at: float | None = None,
    ended_at: float | None = None,
    completion_flag: bool = False,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        id=None,
        session_id=session_id,
        schema_version=SCHEMA_VERSION_CURRENT,
        started_at=started_at if started_at is not None else time.time(),
        ended_at=ended_at,
        events=events,
        completion_flag=completion_flag,
    )


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


def test_apply_pending_creates_tables():
    """Fresh in-memory DB: apply_pending creates expected tables."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "trajectory_records" in tables
    assert "trajectory_events" in tables
    assert "schema_version" in tables
    conn.close()


def test_apply_pending_idempotent():
    """Running apply_pending twice: second run returns [] without error."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)
    result = apply_pending(conn)
    assert result == []
    conn.close()


def test_apply_pending_records_version():
    """After first run schema_version has a row with version=1 and applied_at > 0."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)
    row = conn.execute("SELECT version, applied_at FROM schema_version").fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] > 0
    conn.close()


def test_init_db_returns_connection(tmp_path):
    """init_db with a fresh file DB opens, migrates and returns a connection."""
    db_path = tmp_path / "traj.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    returned = init_db(conn)
    assert returned is conn
    # Should have trajectory_records table
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "trajectory_records" in tables
    conn.close()


# ---------------------------------------------------------------------------
# Insert / get round-trip
# ---------------------------------------------------------------------------


def test_insert_and_get_record_roundtrip(db):
    """Insert a record with 2 events, fetch back; all fields must match."""
    t = time.time()
    ev1 = _make_event(session_id="sess-rt", action_type="tool_call", tool_name="Bash",
                      outcome="success", timestamp=t, metadata={"exit_code": 0})
    ev2 = _make_event(session_id="sess-rt", action_type="user_reply", tool_name=None,
                      outcome="success", timestamp=t + 1.0, metadata={})
    record = _make_record(session_id="sess-rt", events=(ev1, ev2), started_at=t,
                          ended_at=t + 5.0, completion_flag=True)

    rec_id = insert_record(record, conn=db)
    fetched = get_record(rec_id, conn=db)

    assert fetched is not None
    assert fetched.id == rec_id
    assert fetched.session_id == "sess-rt"
    assert fetched.schema_version == SCHEMA_VERSION_CURRENT
    assert fetched.started_at == pytest.approx(t)
    assert fetched.ended_at == pytest.approx(t + 5.0)
    assert fetched.completion_flag is True
    assert len(fetched.events) == 2

    # Check event 0
    fe0 = fetched.events[0]
    assert fe0.action_type == "tool_call"
    assert fe0.tool_name == "Bash"
    assert fe0.outcome == "success"
    assert fe0.metadata == {"exit_code": 0}

    # Check event 1
    fe1 = fetched.events[1]
    assert fe1.action_type == "user_reply"
    assert fe1.tool_name is None
    assert fe1.outcome == "success"


def test_insert_returns_id(db):
    """insert_record returns a positive int; the record's id was None before insert."""
    record = _make_record()
    assert record.id is None
    rec_id = insert_record(record, conn=db)
    assert isinstance(rec_id, int)
    assert rec_id > 0


def test_get_record_not_found_returns_none(db):
    """get_record with a non-existent id returns None."""
    result = get_record(99999, conn=db)
    assert result is None


# ---------------------------------------------------------------------------
# list_recent
# ---------------------------------------------------------------------------


def test_list_recent_orders_descending(db):
    """Insert 3 records with different created_at; list_recent returns newest first."""
    base = time.time()
    for i in range(3):
        rec = _make_record(session_id=f"sess-{i}", started_at=base + i)
        insert_record(rec, conn=db)
        # Bump created_at by updating directly so ordering is deterministic
        db.execute(
            "UPDATE trajectory_records SET created_at = ? WHERE session_id = ?",
            (base + i, f"sess-{i}"),
        )

    records = list_recent(10, conn=db)
    assert len(records) == 3
    # Newest (sess-2, created_at = base+2) should be first
    assert records[0].session_id == "sess-2"
    assert records[2].session_id == "sess-0"


def test_list_recent_respects_limit(db):
    """Insert 5 records; list_recent(2) returns at most 2."""
    for i in range(5):
        insert_record(_make_record(session_id=f"sess-{i}"), conn=db)
    records = list_recent(2, conn=db)
    assert len(records) == 2


# ---------------------------------------------------------------------------
# count_records
# ---------------------------------------------------------------------------


def test_count_records(db):
    """Insert 4 records; count_records() == 4."""
    for i in range(4):
        insert_record(_make_record(session_id=f"sess-{i}"), conn=db)
    assert count_records(conn=db) == 4


# ---------------------------------------------------------------------------
# purge_older_than
# ---------------------------------------------------------------------------


def test_purge_older_than(db):
    """Insert 3 records with ended_at 100/200/300; purge_older_than(250) removes 2."""
    for ended_at in (100.0, 200.0, 300.0):
        rec = _make_record(
            session_id=f"sess-{int(ended_at)}",
            started_at=ended_at - 10,
            ended_at=ended_at,
        )
        insert_record(rec, conn=db)

    deleted = purge_older_than(250.0, conn=db)
    assert deleted == 2
    assert count_records(conn=db) == 1


# ---------------------------------------------------------------------------
# update_reward
# ---------------------------------------------------------------------------


def test_update_reward(db):
    """Insert record, update_reward, verify reward_score stored in DB."""
    rec_id = insert_record(_make_record(), conn=db)
    update_reward(rec_id, 0.75, conn=db)

    row = db.execute(
        "SELECT reward_score FROM trajectory_records WHERE id = ?", (rec_id,)
    ).fetchone()
    assert row is not None
    assert row["reward_score"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Metadata JSON round-trip
# ---------------------------------------------------------------------------


def test_metadata_json_roundtrip(db):
    """Insert event with complex metadata; fetch, metadata equality preserved."""
    metadata = {"count": 42, "items": [1, 2, 3]}
    ev = _make_event(metadata=metadata)
    record = _make_record(events=(ev,))
    rec_id = insert_record(record, conn=db)
    fetched = get_record(rec_id, conn=db)

    assert fetched is not None
    assert len(fetched.events) == 1
    fetched_meta = fetched.events[0].metadata
    assert fetched_meta["count"] == 42
    assert fetched_meta["items"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Path helpers (monkeypatch OPENCOMPUTER_HOME)
# ---------------------------------------------------------------------------


def test_evolution_home_path_uses_home_helper(tmp_path, monkeypatch):
    """evolution_home() returns tmp_path / 'evolution' when OPENCOMPUTER_HOME is set."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    # Re-import to force re-evaluation — _home() reads env at call time
    from opencomputer.evolution import storage as _storage

    result = _storage.evolution_home()
    assert result == tmp_path / "evolution"
    assert result.exists()


def test_trajectory_db_path(tmp_path, monkeypatch):
    """trajectory_db_path() returns the expected path when OPENCOMPUTER_HOME is set."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.evolution import storage as _storage

    result = _storage.trajectory_db_path()
    assert result == tmp_path / "evolution" / "trajectory.sqlite"


# ---------------------------------------------------------------------------
# Foreign key CASCADE
# ---------------------------------------------------------------------------


def test_foreign_key_cascade_on_record_delete(db):
    """Deleting a record row cascades to delete its events (FK ON DELETE CASCADE)."""
    ev1 = _make_event()
    ev2 = _make_event(action_type="user_reply", tool_name=None)
    record = _make_record(events=(ev1, ev2))
    rec_id = insert_record(record, conn=db)

    # Verify events exist
    event_count_before = db.execute(
        "SELECT COUNT(*) FROM trajectory_events WHERE record_id = ?", (rec_id,)
    ).fetchone()[0]
    assert event_count_before == 2

    # Delete the record row directly
    with db:
        db.execute("DELETE FROM trajectory_records WHERE id = ?", (rec_id,))

    # Events should be gone via CASCADE
    event_count_after = db.execute(
        "SELECT COUNT(*) FROM trajectory_events WHERE record_id = ?", (rec_id,)
    ).fetchone()[0]
    assert event_count_after == 0
