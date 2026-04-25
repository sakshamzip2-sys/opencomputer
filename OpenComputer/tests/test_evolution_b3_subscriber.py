"""Integration tests for B3 bus subscriber — opencomputer.evolution.trajectory.

Tests verify:
- register_with_bus() returns a Subscription
- ToolCallEvent fan-out populates _open_trajectories
- Multiple events on the same session_id accumulate
- session_id=None events are silently dropped
- Subscriber exceptions are swallowed + logged (not propagated)
- _on_session_end persists to DB with completion_flag=True and a reward_score
- is_collection_enabled / set_collection_enabled flag file semantics
- bootstrap_if_enabled returns None when disabled, Subscription when enabled

All tests use isolated OPENCOMPUTER_HOME (monkeypatch) and a fresh bus so
they cannot pollute the real user profile or the module-level default bus.
"""

from __future__ import annotations

import time

import pytest

from opencomputer.ingestion.bus import TypedEventBus, reset_default_bus
from plugin_sdk.ingestion import ToolCallEvent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    """Redirect OPENCOMPUTER_HOME to a fresh tmp dir for every test."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def fresh_bus():
    """Return a brand-new TypedEventBus (NOT the module singleton) and reset the
    module singleton so tests that call get_default_bus() also get a clean slate.

    Restores the original singleton after the test so that test ordering
    does not break tests that rely on ``default_bus is get_default_bus()``.
    """
    import opencomputer.ingestion.bus as _bus_mod

    original = _bus_mod.default_bus
    bus = reset_default_bus()
    yield bus
    # Restore the original singleton so other tests see a consistent module attr
    _bus_mod.default_bus = original


@pytest.fixture(autouse=True)
def clear_open_trajectories():
    """Wipe the module-level _open_trajectories dict before each test."""
    from opencomputer.evolution import trajectory as traj_mod

    traj_mod._open_trajectories.clear()
    yield
    traj_mod._open_trajectories.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_call_event(
    session_id: str | None = "sess-test",
    tool_name: str = "Read",
    outcome: str = "success",
    duration: float = 0.1,
    metadata: dict | None = None,
) -> ToolCallEvent:
    return ToolCallEvent(
        session_id=session_id,
        tool_name=tool_name,
        outcome=outcome,  # type: ignore[arg-type]
        duration_seconds=duration,
        source="test",
        metadata=metadata or {},
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# 1. register_with_bus_returns_subscription
# ---------------------------------------------------------------------------


def test_register_with_bus_returns_subscription(fresh_bus):
    """register_with_bus() returns a Subscription; unsubscribe() is callable."""
    from opencomputer.evolution.trajectory import register_with_bus
    from opencomputer.ingestion.bus import Subscription

    sub = register_with_bus(bus=fresh_bus)
    assert isinstance(sub, Subscription)
    # unsubscribe must not raise
    sub.unsubscribe()
    # After unsubscribing, the bus should have no matching subscribers
    remaining = fresh_bus.subscribers("tool_call")
    assert all(s.id != sub.id for s in remaining)


# ---------------------------------------------------------------------------
# 2. test_tool_call_event_appends_to_trajectory
# ---------------------------------------------------------------------------


def test_tool_call_event_appends_to_trajectory(fresh_bus):
    """Publishing a ToolCallEvent populates _open_trajectories with 1 event."""
    from opencomputer.evolution import trajectory as traj_mod
    from opencomputer.evolution.trajectory import register_with_bus

    register_with_bus(bus=fresh_bus)
    event = _tool_call_event(session_id="sess-single")
    fresh_bus.publish(event)

    assert "sess-single" in traj_mod._open_trajectories
    record = traj_mod._open_trajectories["sess-single"]
    assert len(record.events) == 1
    assert record.events[0].tool_name == "Read"
    assert record.events[0].outcome == "success"


# ---------------------------------------------------------------------------
# 3. test_multiple_events_same_session_accumulate
# ---------------------------------------------------------------------------


def test_multiple_events_same_session_accumulate(fresh_bus):
    """Three events with the same session_id produce a single trajectory with 3 events."""
    from opencomputer.evolution import trajectory as traj_mod
    from opencomputer.evolution.trajectory import register_with_bus

    register_with_bus(bus=fresh_bus)
    sid = "sess-multi"
    for tool in ("Read", "Write", "Bash"):
        fresh_bus.publish(_tool_call_event(session_id=sid, tool_name=tool))

    assert sid in traj_mod._open_trajectories
    record = traj_mod._open_trajectories[sid]
    assert len(record.events) == 3
    tool_names = [e.tool_name for e in record.events]
    assert tool_names == ["Read", "Write", "Bash"]


# ---------------------------------------------------------------------------
# 4. test_events_with_none_session_id_dropped
# ---------------------------------------------------------------------------


def test_events_with_none_session_id_dropped(fresh_bus):
    """Events with session_id=None are silently dropped; _open_trajectories stays empty."""
    from opencomputer.evolution import trajectory as traj_mod
    from opencomputer.evolution.trajectory import register_with_bus

    register_with_bus(bus=fresh_bus)
    fresh_bus.publish(_tool_call_event(session_id=None))

    assert len(traj_mod._open_trajectories) == 0


# ---------------------------------------------------------------------------
# 5. test_subscriber_swallows_exceptions
# ---------------------------------------------------------------------------


def test_subscriber_swallows_exceptions(fresh_bus, monkeypatch):
    """If with_event raises, the exception is logged + swallowed; caller unaffected."""
    from opencomputer.evolution import trajectory as traj_mod
    from opencomputer.evolution.trajectory import register_with_bus

    register_with_bus(bus=fresh_bus)

    # Patch with_event to raise after we've seeded one record (so the
    # existing-record code path is triggered and raises)
    sid = "sess-boom"
    # Pre-seed a record so the existing-branch is taken
    from opencomputer.evolution.trajectory import new_record

    traj_mod._open_trajectories[sid] = new_record(sid)

    original_with_event = traj_mod.with_event

    def raising_with_event(record, event):
        raise RuntimeError("Intentional test error")

    monkeypatch.setattr(traj_mod, "with_event", raising_with_event)

    # publish must NOT raise
    fresh_bus.publish(_tool_call_event(session_id=sid))

    # Restore is automatic via monkeypatch; verify no propagation happened
    monkeypatch.setattr(traj_mod, "with_event", original_with_event)


# ---------------------------------------------------------------------------
# 6. test_on_session_end_persists_to_db
# ---------------------------------------------------------------------------


def test_on_session_end_persists_to_db(fresh_bus):
    """Accumulate 2 events then call _on_session_end; record is in DB with correct shape."""
    import sqlite3

    from opencomputer.evolution.storage import init_db, list_recent
    from opencomputer.evolution.trajectory import _on_session_end, register_with_bus

    # Ensure the DB schema is bootstrapped before _on_session_end tries to write
    init_db().close()

    register_with_bus(bus=fresh_bus)
    sid = "sess-persist"
    fresh_bus.publish(_tool_call_event(session_id=sid, tool_name="Read"))
    fresh_bus.publish(_tool_call_event(session_id=sid, tool_name="Write"))

    record_id = _on_session_end(sid)
    assert record_id is not None
    assert isinstance(record_id, int)
    assert record_id > 0

    # Verify the record landed in the DB
    records = list_recent(limit=5)
    assert any(r.id == record_id for r in records)

    persisted = next(r for r in records if r.id == record_id)
    assert len(persisted.events) == 2
    assert persisted.completion_flag is True
    assert persisted.ended_at is not None

    # Verify reward_score was written
    from opencomputer.evolution.storage import trajectory_db_path

    conn = sqlite3.connect(str(trajectory_db_path()))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT reward_score FROM trajectory_records WHERE id = ?", (record_id,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["reward_score"] is not None


# ---------------------------------------------------------------------------
# 7. test_on_session_end_returns_none_for_unknown_session
# ---------------------------------------------------------------------------


def test_on_session_end_returns_none_for_unknown_session():
    """_on_session_end with a session_id never accumulated returns None."""
    from opencomputer.evolution.trajectory import _on_session_end

    result = _on_session_end("sess-never-existed-xyz")
    assert result is None


# ---------------------------------------------------------------------------
# 8. test_is_collection_enabled_default_false
# ---------------------------------------------------------------------------


def test_is_collection_enabled_default_false():
    """Fresh home → is_collection_enabled() == False (no flag file)."""
    from opencomputer.evolution.trajectory import is_collection_enabled

    assert is_collection_enabled() is False


# ---------------------------------------------------------------------------
# 9. test_set_collection_enabled_creates_flag
# ---------------------------------------------------------------------------


def test_set_collection_enabled_creates_flag(isolated_home):
    """set_collection_enabled(True) creates the flag file; False removes it."""
    from opencomputer.evolution.trajectory import is_collection_enabled, set_collection_enabled

    set_collection_enabled(True)
    flag = isolated_home / "evolution" / "enabled"
    assert flag.exists()
    assert is_collection_enabled() is True

    set_collection_enabled(False)
    assert not flag.exists()
    assert is_collection_enabled() is False


# ---------------------------------------------------------------------------
# 10. test_bootstrap_if_enabled_returns_none_when_disabled
# ---------------------------------------------------------------------------


def test_bootstrap_if_enabled_returns_none_when_disabled():
    """bootstrap_if_enabled() returns None when the flag file is absent."""
    from opencomputer.evolution.trajectory import bootstrap_if_enabled

    result = bootstrap_if_enabled()
    assert result is None


# ---------------------------------------------------------------------------
# 11. test_bootstrap_if_enabled_subscribes_when_enabled
# ---------------------------------------------------------------------------


def test_bootstrap_if_enabled_subscribes_when_enabled(fresh_bus, monkeypatch):
    """bootstrap_if_enabled() returns a Subscription when the flag is set."""
    from opencomputer.evolution.trajectory import (
        bootstrap_if_enabled,
        register_with_bus,
        set_collection_enabled,
    )
    from opencomputer.ingestion.bus import Subscription

    set_collection_enabled(True)

    # Patch register_with_bus to use our fresh_bus instead of the default
    def patched_register(bus=None):
        return register_with_bus(bus=fresh_bus)

    monkeypatch.setattr(
        "opencomputer.evolution.trajectory.register_with_bus", patched_register
    )

    result = bootstrap_if_enabled()
    assert result is not None
    assert isinstance(result, Subscription)
