"""delegate-lineage (2026-05-10) — schema, store, registry, and CLI.

Closes the audit defects from `~/.claude/projects/-Users-saksham-Vscode-claude/`
session 655e3c7b... and follow-ups:

1. ``DelegationCompleteEvent.parent_session_id`` was published as ``""``.
2. ``sessions`` table had no ``parent_session_id`` column.
3. ``SubagentRegistry`` was RAM-only.
4. Orchestrator-role demotion was a silent WARNING.
5. ``OPENCOMPUTER_DELEGATION_*`` env vars leaked across delegations.

Each defect gets a focused test (or pair) here.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.config import (
    DelegationConfig,
    LoopConfig,
    default_config,
)
from opencomputer.agent.state import SCHEMA_VERSION, SessionDB
from opencomputer.agent.subagent_registry import SubagentRegistry
from opencomputer.agent.subagent_store import (
    StoredSubagent,
    SubagentStore,
    SubagentStoreUnavailable,
)
from opencomputer.tools.delegate import DelegateTool
from plugin_sdk.core import ToolCall

# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Detach any prior store + clear records so tests don't share state."""
    reg = SubagentRegistry.instance()
    reg.detach_store()
    reg.reset()
    yield
    reg.detach_store()
    reg.reset()


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Per-test sqlite path."""
    return tmp_path / "sessions.db"


@pytest.fixture
def session_db(tmp_db_path: Path) -> SessionDB:
    """A SessionDB at the latest schema (migrations have run)."""
    return SessionDB(tmp_db_path)


@pytest.fixture
def store(session_db: SessionDB) -> SubagentStore:
    """A SubagentStore wired to session_db's path."""
    return SubagentStore(session_db.db_path)


@pytest.fixture
def attached_registry(store: SubagentStore) -> SubagentRegistry:
    reg = SubagentRegistry.instance()
    reg.attach_store(store)
    return reg


# ─── M1 — Schema migration ───────────────────────────────────────────


def test_fresh_db_lands_at_v16(tmp_db_path: Path) -> None:
    db = SessionDB(tmp_db_path)
    with sqlite3.connect(db.db_path) as conn:
        version = conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
    assert version == SCHEMA_VERSION
    assert version >= 16


def test_sessions_has_parent_session_id_column(session_db: SessionDB) -> None:
    with sqlite3.connect(session_db.db_path) as conn:
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
    assert "parent_session_id" in cols


def test_subagents_table_exists_with_required_columns(
    session_db: SessionDB,
) -> None:
    with sqlite3.connect(session_db.db_path) as conn:
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(subagents)").fetchall()
        }
    expected = {
        "agent_id",
        "parent_session_id",
        "child_session_id",
        "parent_agent_id",
        "goal",
        "started_at",
        "ended_at",
        "state",
        "error",
        "role",
        "agent_template",
        "isolation_mode",
        "depth",
        "host_pid",
        "host_started_at",
    }
    assert expected <= cols, f"missing columns: {expected - cols}"


def test_subagents_indexes_present(session_db: SessionDB) -> None:
    with sqlite3.connect(session_db.db_path) as conn:
        idx_names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "idx_sessions_parent" in idx_names
    assert "idx_subagents_parent_session" in idx_names
    assert "idx_subagents_child_session" in idx_names
    assert "idx_subagents_state" in idx_names


def test_v15_db_migrates_to_v16_preserving_existing_rows(
    tmp_db_path: Path,
) -> None:
    """A pre-v16 DB with rows in ``sessions`` migrates cleanly."""
    # Build a synthetic v15-shaped DB: minimal schema_version + sessions
    # row, no parent_session_id column. The migration will ALTER it in.
    with sqlite3.connect(tmp_db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version(version) VALUES (15);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                platform TEXT NOT NULL DEFAULT 'cli',
                model TEXT,
                title TEXT,
                message_count INTEGER DEFAULT 0
            );
            INSERT INTO sessions (id, started_at, platform)
            VALUES ('legacy-session', 12345.0, 'cli');
            """
        )

    # Now opening via SessionDB runs migrations 15→16.
    db = SessionDB(tmp_db_path)

    with sqlite3.connect(db.db_path) as conn:
        version = conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        legacy = conn.execute(
            "SELECT id, parent_session_id FROM sessions WHERE id='legacy-session'"
        ).fetchone()
    assert version == SCHEMA_VERSION
    assert "parent_session_id" in cols
    assert legacy is not None
    assert legacy[0] == "legacy-session"
    assert legacy[1] is None  # NULL preserved on migration


def test_create_session_persists_parent_session_id(session_db: SessionDB) -> None:
    session_db.create_session("child-1", parent_session_id="parent-1")
    row = session_db.get_session("child-1")
    assert row is not None
    assert row["parent_session_id"] == "parent-1"


def test_ensure_session_persists_parent_session_id(
    session_db: SessionDB,
) -> None:
    session_db.ensure_session("child-2", parent_session_id="parent-2")
    row = session_db.get_session("child-2")
    assert row is not None
    assert row["parent_session_id"] == "parent-2"


def test_create_session_does_not_clear_existing_parent_on_upsert(
    session_db: SessionDB,
) -> None:
    """Re-creating an existing session row must not erase parent linkage."""
    session_db.create_session("child-3", parent_session_id="parent-3")
    # Caller re-fires create_session without parent_session_id (e.g.
    # session resume path) — COALESCE preserves the stored value.
    session_db.create_session("child-3", parent_session_id=None)
    row = session_db.get_session("child-3")
    assert row is not None
    assert row["parent_session_id"] == "parent-3"


def test_find_children_sessions_returns_only_matching_parent(
    session_db: SessionDB,
) -> None:
    session_db.create_session("p1")
    session_db.create_session("c1", parent_session_id="p1")
    session_db.create_session("c2", parent_session_id="p1")
    session_db.create_session("c3", parent_session_id="other")
    children = session_db.find_children_sessions("p1")
    ids = {c["id"] for c in children}
    assert ids == {"c1", "c2"}


def test_find_children_sessions_empty_input_returns_empty_list(
    session_db: SessionDB,
) -> None:
    assert session_db.find_children_sessions("") == []


def test_find_root_session_climbs_chain(session_db: SessionDB) -> None:
    session_db.create_session("root")
    session_db.create_session("mid", parent_session_id="root")
    session_db.create_session("leaf", parent_session_id="mid")
    assert session_db.find_root_session("leaf") == "root"
    assert session_db.find_root_session("mid") == "root"
    assert session_db.find_root_session("root") == "root"


def test_find_root_session_terminates_on_corrupt_cycle(
    session_db: SessionDB,
) -> None:
    """A self-referential row must NOT loop forever — max_climb caps it."""
    session_db.create_session("self-ref", parent_session_id="self-ref")
    result = session_db.find_root_session("self-ref", max_climb=3)
    # Whatever the exact value, the call must terminate. 'self-ref' is
    # the only valid value here (it points to itself).
    assert result == "self-ref"


# ─── M3 — SubagentStore ──────────────────────────────────────────────


def test_store_unavailable_on_old_db(tmp_path: Path) -> None:
    """Constructing a store against a DB that lacks subagents fails clean."""
    bad_path = tmp_path / "old.db"
    with sqlite3.connect(bad_path) as conn:
        conn.execute("CREATE TABLE foo(a INTEGER)")  # not a real sessions DB
    with pytest.raises(SubagentStoreUnavailable):
        SubagentStore(bad_path)


def test_store_unavailable_on_missing_path(tmp_path: Path) -> None:
    with pytest.raises(SubagentStoreUnavailable):
        SubagentStore(tmp_path / "ghost.db")


def test_store_upsert_roundtrip(store: SubagentStore) -> None:
    started = datetime.now(UTC)
    store.upsert(
        agent_id="sub-aa",
        parent_session_id="sess-parent",
        child_session_id=None,
        parent_agent_id=None,
        goal="explore docs",
        started_at=started,
        state="running",
        role="leaf",
        agent_template="doc-writer",
        isolation_mode="none",
        depth=0,
    )
    rec = store.get("sub-aa")
    assert rec is not None
    assert rec.agent_id == "sub-aa"
    assert rec.parent_session_id == "sess-parent"
    assert rec.goal == "explore docs"
    assert rec.role == "leaf"
    assert rec.agent_template == "doc-writer"
    assert rec.state == "running"
    assert rec.depth == 0


def test_store_rejects_invalid_state(store: SubagentStore) -> None:
    with pytest.raises(ValueError, match="invalid state"):
        store.upsert(
            agent_id="x",
            parent_session_id=None,
            child_session_id=None,
            parent_agent_id=None,
            goal="g",
            started_at=datetime.now(UTC),
            state="weird",
        )


def test_store_rejects_invalid_role(store: SubagentStore) -> None:
    with pytest.raises(ValueError, match="invalid role"):
        store.upsert(
            agent_id="x",
            parent_session_id=None,
            child_session_id=None,
            parent_agent_id=None,
            goal="g",
            started_at=datetime.now(UTC),
            role="conductor",
        )


def test_store_rejects_invalid_isolation_mode(store: SubagentStore) -> None:
    with pytest.raises(ValueError, match="invalid isolation_mode"):
        store.upsert(
            agent_id="x",
            parent_session_id=None,
            child_session_id=None,
            parent_agent_id=None,
            goal="g",
            started_at=datetime.now(UTC),
            isolation_mode="docker",
        )


def test_store_truncates_long_goal(store: SubagentStore) -> None:
    store.upsert(
        agent_id="long",
        parent_session_id=None,
        child_session_id=None,
        parent_agent_id=None,
        goal="x" * 500,
        started_at=datetime.now(UTC),
    )
    rec = store.get("long")
    assert rec is not None
    assert len(rec.goal) == 200


def test_store_update_partial_fields(store: SubagentStore) -> None:
    started = datetime.now(UTC)
    store.upsert(
        agent_id="sub-bb",
        parent_session_id="p",
        child_session_id=None,
        parent_agent_id=None,
        goal="g",
        started_at=started,
    )
    ended = started + timedelta(seconds=5)
    store.update("sub-bb", state="completed", ended_at=ended)
    rec = store.get("sub-bb")
    assert rec is not None
    assert rec.state == "completed"
    assert rec.ended_at is not None
    # Allow 100ms tolerance for sqlite REAL roundtrip.
    assert abs((rec.ended_at - ended).total_seconds()) < 0.1


def test_store_update_unknown_field_raises(store: SubagentStore) -> None:
    started = datetime.now(UTC)
    store.upsert(
        agent_id="x",
        parent_session_id=None,
        child_session_id=None,
        parent_agent_id=None,
        goal="g",
        started_at=started,
    )
    with pytest.raises(KeyError, match="unknown subagent field"):
        store.update("x", banana=True)


def test_store_update_silently_ignores_ram_only_fields(
    store: SubagentStore,
) -> None:
    """``current_tool`` and ``tokens_used`` are accepted by the registry's
    update() (they're real fields on SubagentRecord) but the store
    persists neither — they're filtered out before the SQL runs."""
    store.upsert(
        agent_id="x",
        parent_session_id=None,
        child_session_id=None,
        parent_agent_id=None,
        goal="g",
        started_at=datetime.now(UTC),
    )
    # Should NOT raise even though current_tool/tokens_used aren't in SQL.
    store.update("x", current_tool="Read", tokens_used=42)


def test_store_history_filters_running_and_orders_newest_first(
    store: SubagentStore,
) -> None:
    base = datetime.now(UTC)
    for i, name in enumerate(("alpha", "beta", "gamma")):
        store.upsert(
            agent_id=name,
            parent_session_id=None,
            child_session_id=None,
            parent_agent_id=None,
            goal=name,
            started_at=base,
        )
        store.update(
            name, state="completed", ended_at=base + timedelta(seconds=i + 1)
        )
    # Add a still-running record to confirm it's filtered out.
    store.upsert(
        agent_id="running",
        parent_session_id=None,
        child_session_id=None,
        parent_agent_id=None,
        goal="still alive",
        started_at=base,
    )
    history = store.history(limit=10)
    assert {r.agent_id for r in history} == {"alpha", "beta", "gamma"}
    # Newest first
    assert [r.agent_id for r in history] == ["gamma", "beta", "alpha"]


def test_store_list_running_filters_state(store: SubagentStore) -> None:
    base = datetime.now(UTC)
    store.upsert(
        agent_id="r",
        parent_session_id=None,
        child_session_id=None,
        parent_agent_id=None,
        goal="r",
        started_at=base,
    )
    store.upsert(
        agent_id="d",
        parent_session_id=None,
        child_session_id=None,
        parent_agent_id=None,
        goal="d",
        started_at=base,
    )
    store.update("d", state="completed", ended_at=base)
    running = store.list_running()
    assert {r.agent_id for r in running} == {"r"}


def test_store_find_by_parent(store: SubagentStore) -> None:
    base = datetime.now(UTC)
    store.upsert(
        agent_id="c1",
        parent_session_id="parent",
        child_session_id=None,
        parent_agent_id=None,
        goal="c1",
        started_at=base,
    )
    store.upsert(
        agent_id="c2",
        parent_session_id="parent",
        child_session_id=None,
        parent_agent_id=None,
        goal="c2",
        started_at=base + timedelta(seconds=1),
    )
    store.upsert(
        agent_id="c3",
        parent_session_id="other",
        child_session_id=None,
        parent_agent_id=None,
        goal="c3",
        started_at=base,
    )
    rows = store.find_by_parent("parent")
    assert [r.agent_id for r in rows] == ["c1", "c2"]


def test_store_find_by_child(store: SubagentStore) -> None:
    base = datetime.now(UTC)
    store.upsert(
        agent_id="agt-1",
        parent_session_id="p",
        child_session_id="child-sess-X",
        parent_agent_id=None,
        goal="g",
        started_at=base,
    )
    rec = store.find_by_child("child-sess-X")
    assert rec is not None
    assert rec.agent_id == "agt-1"
    assert store.find_by_child("nope") is None


def test_stored_is_orphaned_for_dead_pid(store: SubagentStore) -> None:
    """A 'running' record whose pid is dead is reported orphaned."""
    base = datetime.now(UTC)
    store.upsert(
        agent_id="orphan",
        parent_session_id=None,
        child_session_id=None,
        parent_agent_id=None,
        goal="orphan",
        started_at=base,
    )
    # Manually overwrite host_pid + host_started_at with a value that
    # is virtually certain to be dead. PID 0 is reserved on Unix; on
    # Windows ``os.kill(0, 0)`` raises ``OSError`` for PID 0 too.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE subagents SET host_pid=?, host_started_at=? WHERE agent_id=?",
            (1, 1.0, "orphan"),
        )
        conn.commit()
    rec = store.get("orphan")
    assert rec is not None
    # PID 1 is init/launchd — alive on every host. We need a pid that's
    # guaranteed dead. Use a sentinel by writing a non-existent pid.
    # Sqlite's INTEGER fits 64 bits; pick something extreme.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE subagents SET host_pid=? WHERE agent_id=?",
            (2_000_000_000, "orphan"),  # implausibly high; not in use
        )
        conn.commit()
    rec = store.get("orphan")
    assert rec is not None
    assert rec.is_orphaned is True
    assert rec.display_state == "orphaned"


def test_stored_not_orphaned_when_completed(store: SubagentStore) -> None:
    """Terminal states never report orphaned regardless of pid status."""
    base = datetime.now(UTC)
    store.upsert(
        agent_id="x",
        parent_session_id=None,
        child_session_id=None,
        parent_agent_id=None,
        goal="x",
        started_at=base,
    )
    store.update("x", state="completed", ended_at=base)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE subagents SET host_pid=? WHERE agent_id=?",
            (2_000_000_000, "x"),
        )
        conn.commit()
    rec = store.get("x")
    assert rec is not None
    assert rec.is_orphaned is False


def test_store_reset_clears_table(store: SubagentStore) -> None:
    base = datetime.now(UTC)
    store.upsert(
        agent_id="x",
        parent_session_id=None,
        child_session_id=None,
        parent_agent_id=None,
        goal="x",
        started_at=base,
    )
    assert store.get("x") is not None
    store.reset()
    assert store.get("x") is None


# ─── Registry write-through ──────────────────────────────────────────


def test_registry_writes_through_when_store_attached(
    attached_registry: SubagentRegistry,
    store: SubagentStore,
) -> None:
    rec = attached_registry.register(
        parent_id=None,
        goal="hello",
        parent_session_id="parent-X",
    )
    persisted = store.get(rec.agent_id)
    assert persisted is not None
    assert persisted.parent_session_id == "parent-X"
    assert persisted.goal == "hello"


def test_registry_history_survives_reset_when_store_persists(
    attached_registry: SubagentRegistry,
    store: SubagentStore,
) -> None:
    """Cross-process simulation: register a record in process A,
    "restart" by clearing the in-memory dict only, then read history
    via a fresh registry attached to the same store. The completed
    record should still be visible."""
    rec = attached_registry.register(parent_id=None, goal="finished task")
    attached_registry.update(
        rec.agent_id, state="completed", ended_at=datetime.now(UTC)
    )

    # Simulate fresh process: reset() ALSO wipes the store, so we
    # instead use a manual in-memory dict clear + reattach.
    attached_registry._records.clear()  # noqa: SLF001 — simulate restart

    history = attached_registry.history()
    assert len(history) == 1
    assert history[0].agent_id == rec.agent_id
    assert history[0].state == "completed"


def test_registry_register_invalid_role_raises(
    attached_registry: SubagentRegistry,
) -> None:
    with pytest.raises(ValueError, match="role must be"):
        attached_registry.register(parent_id=None, goal="g", role="boss")


def test_registry_register_invalid_isolation_mode_raises(
    attached_registry: SubagentRegistry,
) -> None:
    with pytest.raises(ValueError, match="isolation_mode must be"):
        attached_registry.register(
            parent_id=None, goal="g", isolation_mode="podman"
        )


def test_registry_kill_persists_state(
    attached_registry: SubagentRegistry,
    store: SubagentStore,
) -> None:
    rec = attached_registry.register(parent_id=None, goal="g")
    assert attached_registry.kill(rec.agent_id) is True
    persisted = store.get(rec.agent_id)
    assert persisted is not None
    assert persisted.state == "killed"
    assert persisted.ended_at is not None


def test_registry_attach_detach_idempotent(
    attached_registry: SubagentRegistry,
    store: SubagentStore,
) -> None:
    # Re-attaching the same store is a no-op.
    attached_registry.attach_store(store)
    assert attached_registry.has_store() is True
    attached_registry.detach_store()
    assert attached_registry.has_store() is False
    # Detach again is harmless.
    attached_registry.detach_store()
    assert attached_registry.has_store() is False


def test_registry_works_without_store_attached() -> None:
    """Back-compat: with no store, register/update/history are RAM-only."""
    reg = SubagentRegistry.instance()
    reg.detach_store()
    reg.reset()
    rec = reg.register(parent_id=None, goal="ram-only")
    reg.update(rec.agent_id, state="completed", ended_at=datetime.now(UTC))
    history = reg.history()
    assert len(history) == 1
    assert history[0].state == "completed"


# ─── M2 — Plumbing through delegate.py ──────────────────────────────


def _build_parent_with_session_id(session_id: str) -> MagicMock:
    cfg = default_config()
    parent = MagicMock()
    parent.config = cfg
    parent._current_session_id = session_id
    return parent


def _build_child_loop(success_text: str = "ok"):
    cfg = default_config()
    fake = MagicMock()
    fake.config = cfg
    fake.allowed_tools = None
    fake_msg = MagicMock(content=success_text)
    fake_result = MagicMock(final_message=fake_msg, session_id="child-sess-1")
    fake.run_conversation = AsyncMock(return_value=fake_result)
    return fake


@pytest.mark.asyncio
async def test_register_carries_parent_session_id_when_parent_loop_has_one(
    attached_registry: SubagentRegistry,
    store: SubagentStore,
) -> None:
    parent = _build_parent_with_session_id("parent-sess-A")
    child = _build_child_loop()

    tool = DelegateTool()
    factory = MagicMock(return_value=child)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    call = ToolCall(id="cx", name="delegate", arguments={"task": "explore"})
    await tool.execute(call)

    # Find the persisted record — only one was registered this test.
    rows = store.list_running() + store.history(limit=10)
    matches = [r for r in rows if r.parent_session_id == "parent-sess-A"]
    assert matches, f"no record carries parent_session_id; rows={rows}"
    assert matches[0].child_session_id == "child-sess-1"


@pytest.mark.asyncio
async def test_register_falls_back_to_empty_when_parent_loop_lacks_session(
    attached_registry: SubagentRegistry,
    store: SubagentStore,
) -> None:
    """A MagicMock without `_current_session_id` set explicitly must NOT
    poison the column with a MagicMock repr."""
    cfg = default_config()
    parent = MagicMock(spec=["config", "_current_session_id"])  # constrained
    parent.config = cfg
    # _current_session_id is unset → MagicMock returns... a MagicMock.
    # Our defensive isinstance check should default it to "".
    child = _build_child_loop()
    tool = DelegateTool()
    factory = MagicMock(return_value=child)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    call = ToolCall(id="cy", name="delegate", arguments={"task": "x"})
    await tool.execute(call)

    rows = store.list_running() + store.history(limit=10)
    assert rows, "no records persisted"
    # Either NULL or "" (the registry stores "" → store stores it as NULL)
    for r in rows:
        assert r.parent_session_id in (None, ""), (
            f"non-string parent_session_id leaked: {r.parent_session_id!r}"
        )


@pytest.mark.asyncio
async def test_delegation_event_carries_parent_session_id() -> None:
    """When a parent loop has a session_id, the bus event reflects it."""
    from opencomputer.ingestion.bus import default_bus
    from plugin_sdk.ingestion import DelegationCompleteEvent

    parent = _build_parent_with_session_id("parent-sess-EV")
    child = _build_child_loop()

    captured: list[DelegationCompleteEvent] = []

    def _on_event(ev: DelegationCompleteEvent) -> None:
        captured.append(ev)

    sub = default_bus.subscribe("delegation_complete", _on_event)
    try:
        tool = DelegateTool()
        factory = MagicMock(return_value=child)
        factory.__self__ = parent
        DelegateTool.set_factory(factory, instance=tool)

        call = ToolCall(id="bus", name="delegate", arguments={"task": "bus"})
        await tool.execute(call)
    finally:
        sub.unsubscribe()

    matches = [
        ev for ev in captured if ev.parent_session_id == "parent-sess-EV"
    ]
    assert matches, f"no DelegationCompleteEvent carries the parent id; got {captured}"
    assert matches[0].child_session_id == "child-sess-1"
    assert matches[0].child_outcome in ("success", "failure")


@pytest.mark.asyncio
async def test_child_runtime_custom_carries_parent_session_id_for_child_loop_to_persist() -> None:
    """The child runtime received by run_conversation MUST carry
    parent_session_id in custom dict so the child loop can write it
    onto the sessions row. We assert by inspecting the runtime that
    was passed to the (mocked) child."""
    parent = _build_parent_with_session_id("parent-sess-RUNTIME")
    child = _build_child_loop()

    tool = DelegateTool()
    factory = MagicMock(return_value=child)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    call = ToolCall(id="rt", name="delegate", arguments={"task": "rt"})
    await tool.execute(call)

    child.run_conversation.assert_called_once()
    kwargs = child.run_conversation.call_args.kwargs
    runtime = kwargs.get("runtime")
    assert runtime is not None, "child must receive a runtime"
    assert (runtime.custom or {}).get("parent_session_id") == "parent-sess-RUNTIME"


# ─── M5 — bombs ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_demotion_surfaces_in_result_content() -> None:
    """role=orchestrator at max_depth must be demoted AND surfaced."""
    import dataclasses as _dc

    parent = _build_parent_with_session_id("parent-sess-DEMOTE")
    # Force the parent's max_delegation_depth to 1 so any orchestrator
    # request is at the boundary (depth 0 + 1 >= 1). Config is frozen
    # so we ``replace`` to build the override.
    parent.config = _dc.replace(
        parent.config,
        loop=_dc.replace(parent.config.loop, max_delegation_depth=1),
    )

    child = _build_child_loop(success_text="task body output")

    tool = DelegateTool()
    factory = MagicMock(return_value=child)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    call = ToolCall(
        id="dm",
        name="delegate",
        arguments={"task": "do thing", "role": "orchestrator"},
    )
    result = await tool.execute(call)

    assert result.is_error is None or result.is_error is False
    assert isinstance(result.content, str)
    assert result.content.startswith(
        "Note: role=orchestrator was demoted to leaf"
    ), f"missing demotion prefix; got: {result.content[:100]!r}"
    assert "task body output" in result.content


@pytest.mark.asyncio
async def test_orchestrator_demotion_does_not_prefix_when_role_is_leaf() -> None:
    """A normal leaf delegation must not pick up the demotion prefix."""
    parent = _build_parent_with_session_id("parent-leaf")
    child = _build_child_loop(success_text="leaf output")

    tool = DelegateTool()
    factory = MagicMock(return_value=child)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    call = ToolCall(id="lf", name="delegate", arguments={"task": "x"})
    result = await tool.execute(call)
    assert result.content == "leaf output"


@pytest.mark.asyncio
async def test_api_key_env_var_does_not_leak_after_delegation_when_absent() -> None:
    """The bomb #5 fix: env vars must NOT outlive a single delegation."""
    import dataclasses as _dc

    parent = _build_parent_with_session_id("parent-env-A")
    parent.config = _dc.replace(
        parent.config,
        loop=_dc.replace(
            parent.config.loop,
            delegation=DelegationConfig(api_key="secret-test-key"),
        ),
    )

    child = _build_child_loop()

    # Sanity: var must be absent before the call.
    os.environ.pop("OPENCOMPUTER_DELEGATION_API_KEY", None)
    assert "OPENCOMPUTER_DELEGATION_API_KEY" not in os.environ

    tool = DelegateTool()
    factory = MagicMock(return_value=child)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    call = ToolCall(id="env", name="delegate", arguments={"task": "x"})
    await tool.execute(call)

    # And after the call, it must be gone again (the bomb).
    assert (
        "OPENCOMPUTER_DELEGATION_API_KEY" not in os.environ
    ), "OPENCOMPUTER_DELEGATION_API_KEY leaked past delegation lifetime"


@pytest.mark.asyncio
async def test_api_key_env_var_restored_to_prior_value() -> None:
    """If a prior value existed, it must be restored — not popped."""
    import dataclasses as _dc

    os.environ["OPENCOMPUTER_DELEGATION_API_KEY"] = "prior-value"
    try:
        parent = _build_parent_with_session_id("parent-env-B")
        parent.config = _dc.replace(
            parent.config,
            loop=_dc.replace(
                parent.config.loop,
                delegation=DelegationConfig(api_key="override"),
            ),
        )

        child = _build_child_loop()
        tool = DelegateTool()
        factory = MagicMock(return_value=child)
        factory.__self__ = parent
        DelegateTool.set_factory(factory, instance=tool)

        call = ToolCall(id="env2", name="delegate", arguments={"task": "x"})
        await tool.execute(call)
        assert os.environ.get("OPENCOMPUTER_DELEGATION_API_KEY") == "prior-value"
    finally:
        os.environ.pop("OPENCOMPUTER_DELEGATION_API_KEY", None)


# ─── M6 — stress + crash ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kill_during_run_persists_killed_state(
    attached_registry: SubagentRegistry,
    store: SubagentStore,
) -> None:
    """Mid-run kill writes 'killed' to sqlite, not just RAM."""
    rec = attached_registry.register(parent_id=None, goal="alive")
    assert attached_registry.kill(rec.agent_id) is True
    persisted = store.get(rec.agent_id)
    assert persisted is not None
    assert persisted.state == "killed"


@pytest.mark.asyncio
async def test_baseexception_in_child_marks_failed_in_store(
    attached_registry: SubagentRegistry,
    store: SubagentStore,
) -> None:
    """Parent crash mid-delegation marks the child registry record failed."""
    parent = _build_parent_with_session_id("parent-crash")
    child = _build_child_loop()
    child.run_conversation = AsyncMock(side_effect=RuntimeError("boom"))

    tool = DelegateTool()
    factory = MagicMock(return_value=child)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    call = ToolCall(id="boom", name="delegate", arguments={"task": "x"})
    with pytest.raises(RuntimeError, match="boom"):
        await tool.execute(call)

    history = store.history(limit=10)
    matches = [r for r in history if r.parent_session_id == "parent-crash"]
    assert matches, "no failed record landed in sqlite"
    assert matches[0].state == "failed"
    assert "boom" in (matches[0].error or "")


@pytest.mark.asyncio
async def test_real_batch_three_tasks_all_persist_with_parent_session_id(
    attached_registry: SubagentRegistry,
    store: SubagentStore,
) -> None:
    """delegate(tasks=[3 small things]) — all 3 children persist."""
    parent = _build_parent_with_session_id("parent-batch")
    # Make every factory() call return a fresh child mock — needed so
    # asyncio.gather can dispatch all three concurrently without
    # state-sharing on a single child mock.
    childs: list = []

    def _factory_returns_fresh():
        c = _build_child_loop()
        # Each child's session_id must be unique so the test can verify
        # all three persisted distinctly.
        idx = len(childs)
        c.run_conversation.return_value = MagicMock(  # type: ignore[attr-defined]
            final_message=MagicMock(content=f"result-{idx}"),
            session_id=f"child-{idx}",
        )
        childs.append(c)
        return c

    tool = DelegateTool()
    factory = MagicMock(side_effect=_factory_returns_fresh)
    factory.__self__ = parent
    DelegateTool.set_factory(factory, instance=tool)

    call = ToolCall(
        id="batch",
        name="delegate",
        arguments={
            "tasks": [
                {"goal": "task A"},
                {"goal": "task B"},
                {"goal": "task C"},
            ]
        },
    )
    result = await tool.execute(call)
    assert result.is_error is None or result.is_error is False
    assert "task A" not in (result.content or "")  # we map goal→task internally
    assert "result-0" in (result.content or "")
    assert "result-1" in (result.content or "")
    assert "result-2" in (result.content or "")

    # All three records persisted with the parent's session_id.
    history = store.history(limit=20)
    matches = [r for r in history if r.parent_session_id == "parent-batch"]
    assert len(matches) == 3
    assert {m.state for m in matches} == {"completed"}
    # Each carries a distinct child_session_id.
    assert {m.child_session_id for m in matches} == {
        "child-0",
        "child-1",
        "child-2",
    }
