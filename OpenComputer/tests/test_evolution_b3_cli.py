"""CLI tests for B3 commands: trajectories show, enable, disable.

Uses typer.testing.CliRunner with isolated OPENCOMPUTER_HOME so no real
user profile is touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.evolution.entrypoint import evolution_app
from opencomputer.evolution.storage import apply_pending, insert_record
from opencomputer.evolution.trajectory import TrajectoryEvent, TrajectoryRecord

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    """Set OPENCOMPUTER_HOME to a fresh tmp dir and return it."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db_with_records(db_path: Path, count: int = 2) -> list[int]:
    """Create the evolution DB at *db_path* and insert *count* completed records."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)

    ids = []
    for i in range(count):
        ev = TrajectoryEvent(
            session_id=f"sess-{i}",
            message_id=None,
            action_type="tool_call",
            tool_name="Read",
            outcome="success",
            timestamp=float(1_700_000_000 + i),
            metadata={"seq": i},
        )
        rec = TrajectoryRecord(
            id=None,
            session_id=f"sess-{i}",
            schema_version=1,
            started_at=float(1_700_000_000 + i),
            ended_at=float(1_700_000_000 + i + 10),
            events=(ev,),
            completion_flag=True,
        )
        record_id = insert_record(rec, conn=conn)
        ids.append(record_id)

    conn.close()
    return ids


# ---------------------------------------------------------------------------
# 1. test_trajectories_show_empty
# ---------------------------------------------------------------------------


def test_trajectories_show_empty(isolated_home):
    """Fresh home with no records → informative 'No trajectories captured yet' message."""
    result = runner.invoke(evolution_app, ["trajectories", "show"])
    assert result.exit_code == 0, result.output
    assert "No trajectories captured yet" in result.output


# ---------------------------------------------------------------------------
# 2. test_trajectories_show_with_records
# ---------------------------------------------------------------------------


def test_trajectories_show_with_records(isolated_home):
    """Pre-insert 2 records; `trajectories show` renders a table with both rows."""
    evo_dir = isolated_home / "evolution"
    evo_dir.mkdir(parents=True, exist_ok=True)
    db_path = evo_dir / "trajectory.sqlite"
    inserted_ids = _make_db_with_records(db_path, count=2)

    result = runner.invoke(evolution_app, ["trajectories", "show"])
    assert result.exit_code == 0, result.output
    # Both record IDs should appear in the output table
    for rid in inserted_ids:
        assert str(rid) in result.output
    # Table header keywords
    assert "session_id" in result.output or "sess-" in result.output


# ---------------------------------------------------------------------------
# 3. test_enable_creates_flag
# ---------------------------------------------------------------------------


def test_enable_creates_flag(isolated_home):
    """`evolution enable` creates the flag file; is_collection_enabled() == True."""
    from opencomputer.evolution.trajectory import is_collection_enabled

    result = runner.invoke(evolution_app, ["enable"])
    assert result.exit_code == 0, result.output
    assert "enabled" in result.output.lower()
    assert is_collection_enabled() is True


# ---------------------------------------------------------------------------
# 4. test_disable_removes_flag
# ---------------------------------------------------------------------------


def test_disable_removes_flag(isolated_home):
    """`evolution enable` then `evolution disable` → is_collection_enabled() == False."""
    from opencomputer.evolution.trajectory import is_collection_enabled

    runner.invoke(evolution_app, ["enable"])
    assert is_collection_enabled() is True

    result = runner.invoke(evolution_app, ["disable"])
    assert result.exit_code == 0, result.output
    assert "disabled" in result.output.lower()
    assert is_collection_enabled() is False


# ---------------------------------------------------------------------------
# 5. test_enable_idempotent
# ---------------------------------------------------------------------------


def test_enable_idempotent(isolated_home):
    """Calling `evolution enable` twice does not raise and leaves collection enabled."""
    from opencomputer.evolution.trajectory import is_collection_enabled

    result1 = runner.invoke(evolution_app, ["enable"])
    assert result1.exit_code == 0, result1.output

    result2 = runner.invoke(evolution_app, ["enable"])
    assert result2.exit_code == 0, result2.output

    assert is_collection_enabled() is True
