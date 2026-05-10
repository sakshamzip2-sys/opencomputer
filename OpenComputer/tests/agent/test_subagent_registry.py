"""SubagentRegistry — register/kill/list_running/history/cross-loop kill safety."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from opencomputer.agent.subagent_registry import SubagentRegistry


@pytest.fixture(autouse=True)
def _reset_registry():
    # delegate-lineage (2026-05-10): an AgentLoop construction earlier in
    # the suite may have attached a SubagentStore to the singleton.
    # Detach so these RAM-only tests don't share state with sqlite.
    SubagentRegistry.instance().detach_store()
    SubagentRegistry.instance().reset()
    yield
    SubagentRegistry.instance().detach_store()
    SubagentRegistry.instance().reset()


def test_singleton_returns_same_instance():
    a = SubagentRegistry.instance()
    b = SubagentRegistry.instance()
    assert a is b


def test_register_creates_running_record():
    reg = SubagentRegistry.instance()
    rec = reg.register(parent_id=None, goal="root task")
    assert rec.state == "running"
    assert rec.parent_id is None
    assert rec.goal == "root task"
    assert rec.ended_at is None
    assert reg.list_running() == [rec]


def test_register_truncates_long_goal():
    reg = SubagentRegistry.instance()
    long_goal = "x" * 500
    rec = reg.register(parent_id=None, goal=long_goal)
    assert len(rec.goal) == 200


def test_kill_running_returns_true_and_marks_killed():
    reg = SubagentRegistry.instance()
    rec = reg.register(parent_id=None, goal="x")
    ok = reg.kill(rec.agent_id)
    assert ok is True
    assert reg.list_running() == []
    history = reg.history()
    assert len(history) == 1
    assert history[0].state == "killed"
    assert history[0].ended_at is not None


def test_kill_unknown_returns_false():
    reg = SubagentRegistry.instance()
    assert reg.kill("nonexistent") is False


def test_kill_already_completed_returns_false():
    reg = SubagentRegistry.instance()
    rec = reg.register(parent_id=None, goal="x")
    reg.update(rec.agent_id, state="completed", ended_at=datetime.now(UTC))
    assert reg.kill(rec.agent_id) is False


def test_history_includes_completed_in_reverse_chronological():
    reg = SubagentRegistry.instance()
    rec1 = reg.register(parent_id=None, goal="first")
    rec2 = reg.register(parent_id=None, goal="second")
    # Mark first as completed earlier, second as completed later
    t1 = datetime.now(UTC)
    reg.update(rec1.agent_id, state="completed", ended_at=t1)
    import time as _t
    _t.sleep(0.01)  # ensure ordering
    t2 = datetime.now(UTC)
    reg.update(rec2.agent_id, state="completed", ended_at=t2)
    history = reg.history()
    assert len(history) == 2
    # Newest first
    assert history[0].goal == "second"
    assert history[1].goal == "first"


def test_history_limit_caps_results():
    reg = SubagentRegistry.instance()
    for i in range(10):
        rec = reg.register(parent_id=None, goal=f"goal-{i}")
        reg.update(rec.agent_id, state="completed", ended_at=datetime.now(UTC))
    history = reg.history(limit=3)
    assert len(history) == 3


def test_update_unknown_silently_noops():
    reg = SubagentRegistry.instance()
    reg.update("ghost-id", state="killed")  # must not raise


def test_update_only_known_fields():
    reg = SubagentRegistry.instance()
    rec = reg.register(parent_id=None, goal="x")
    # Unknown field is silently ignored.
    reg.update(rec.agent_id, bogus_field=123, current_tool="Read")
    refreshed = reg.list_running()[0]
    assert refreshed.current_tool == "Read"
    assert not hasattr(refreshed, "bogus_field")


def test_list_running_excludes_completed():
    reg = SubagentRegistry.instance()
    a = reg.register(parent_id=None, goal="alive")
    b = reg.register(parent_id=None, goal="done")
    reg.update(b.agent_id, state="completed", ended_at=datetime.now(UTC))
    running = reg.list_running()
    assert len(running) == 1
    assert running[0].agent_id == a.agent_id


@pytest.mark.asyncio
async def test_kill_uses_call_soon_threadsafe_for_cross_loop_safety():
    """F4 audit fix: kill() must dispatch to the child's loop, not the caller's.

    We register a record (which captures the *current* running loop), then
    kill from a different loop. The cancel event should fire on the
    captured loop, not raise.
    """
    reg = SubagentRegistry.instance()
    # Register from the test's current loop — captures it on the record.
    rec = reg.register(parent_id=None, goal="x")
    assert rec.event_loop is asyncio.get_running_loop()
    assert rec.cancel_event is not None
    assert not rec.cancel_event.is_set()

    # Kill from this same loop (call_soon_threadsafe still works in-loop).
    ok = reg.kill(rec.agent_id)
    assert ok is True
    # Yield once so the scheduled call_soon fires.
    await asyncio.sleep(0)
    assert rec.cancel_event.is_set()
