"""PR-8 of Hermes parity: execution-trace metadata + bus-driven memory hooks."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# ─── T3.1: execution trace metadata ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_trajectory_event_propagates_error_class():
    """When a ToolCallEvent has error_class in metadata, the TrajectoryEvent
    keeps it (subject to the 200-char privacy rule)."""
    from opencomputer.evolution.trajectory import (
        _on_tool_call_event,
        _open_trajectories,
    )
    from plugin_sdk.ingestion import ToolCallEvent

    event = ToolCallEvent(
        session_id="sess-pr8-t31",
        source="agent_loop",
        tool_name="Read",
        outcome="failure",
        duration_seconds=0.05,
        metadata={
            "error_class": "FileNotFoundError",
            "error_message_preview": "No such file: /tmp/missing",
        },
    )
    _open_trajectories.clear()
    _on_tool_call_event(event)
    rec = _open_trajectories["sess-pr8-t31"]
    assert len(rec.events) == 1
    ev = rec.events[0]
    assert ev.metadata.get("error_class") == "FileNotFoundError"
    assert "No such file" in ev.metadata.get("error_message_preview", "")


def test_reflect_template_renders_error_info():
    """The reflect.j2 template now includes error_class / error_message_preview when present."""
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader

    from opencomputer.evolution.trajectory import TrajectoryEvent, TrajectoryRecord

    template_dir = Path("opencomputer/evolution/prompts")
    env = Environment(
        loader=FileSystemLoader(template_dir),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = env.get_template("reflect.j2")

    failing_event = TrajectoryEvent(
        session_id="sess-pr8-reflect",
        message_id=None,
        action_type="tool_call",
        tool_name="Read",
        outcome="failure",
        timestamp=1.0,
        metadata={
            "error_class": "FileNotFoundError",
            "error_message_preview": "No such file: /tmp/x",
        },
    )
    record = TrajectoryRecord(
        id=42,
        session_id="sess-pr8-reflect",
        schema_version=1,
        started_at=0.0,
        ended_at=10.0,
        events=(failing_event,),
        completion_flag=False,
    )
    output = template.render(records=[record], model_hint="claude-opus-4-7", now=20.0)
    assert "FileNotFoundError" in output
    assert "No such file" in output


def test_reflect_template_no_error_info_for_success():
    """Events with no error_class do NOT emit the warning prefix."""
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader

    from opencomputer.evolution.trajectory import TrajectoryEvent, TrajectoryRecord

    template_dir = Path("opencomputer/evolution/prompts")
    env = Environment(
        loader=FileSystemLoader(template_dir),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = env.get_template("reflect.j2")

    ok_event = TrajectoryEvent(
        session_id="sess-pr8-ok",
        message_id=None,
        action_type="tool_call",
        tool_name="Read",
        outcome="success",
        timestamp=1.0,
        metadata={"duration_seconds": 0.05},
    )
    record = TrajectoryRecord(
        id=1,
        session_id="sess-pr8-ok",
        schema_version=1,
        started_at=0.0,
        ended_at=10.0,
        events=(ok_event,),
        completion_flag=True,
    )
    output = template.render(records=[record], model_hint="m", now=20.0)
    assert "⚠️" not in output
    assert "error_class" not in output


def test_loop_emit_captures_exception_metadata(tmp_path, monkeypatch):
    """_emit_tool_call_event with exception=... adds error_class + error_message_preview
    to the published ToolCallEvent's metadata."""
    import opencomputer.ingestion.bus as _bus_module
    from opencomputer.agent.config import (
        Config,
        LoopConfig,
        MemoryConfig,
        ModelConfig,
        SessionConfig,
    )
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.state import SessionDB
    from opencomputer.ingestion.bus import TypedEventBus
    from plugin_sdk.core import ToolCall

    # Patch without replacing the singleton so we don't break
    # test_typed_event_bus.py::test_default_bus_is_singleton.
    bus = TypedEventBus()
    monkeypatch.setattr(_bus_module, "default_bus", bus)
    captured_events = []
    bus.subscribe("tool_call", captured_events.append)

    db_path = tmp_path / "test.db"
    config = Config(
        model=ModelConfig(model="gpt-4o"),
        session=SessionConfig(db_path=db_path),
        memory=MemoryConfig(
            declarative_path=tmp_path / "MEMORY.md",
            skills_path=tmp_path / "skills",
        ),
        loop=LoopConfig(),
    )
    provider = MagicMock()
    loop = AgentLoop(provider=provider, config=config, db=SessionDB(db_path))

    fake_call = ToolCall(id="call-1", name="Read", arguments={})
    exc = FileNotFoundError("No such file: /tmp/missing")
    loop._emit_tool_call_event(
        call=fake_call,
        outcome="failure",
        duration_seconds=0.01,
        session_id="sess-x",
        exception=exc,
    )

    assert len(captured_events) == 1
    ev = captured_events[0]
    assert ev.metadata.get("error_class") == "FileNotFoundError"
    assert "No such file" in ev.metadata.get("error_message_preview", "")


# ─── T3.2: bus event publishing ──────────────────────────────────────────────


def test_turn_start_event_class_exists():
    from plugin_sdk.ingestion import TurnStartEvent

    ev = TurnStartEvent(session_id="s1", source="agent_loop", turn_index=3)
    assert ev.event_type == "turn_start"
    assert ev.turn_index == 3


def test_delegation_complete_event_class_exists():
    from plugin_sdk.ingestion import DelegationCompleteEvent

    ev = DelegationCompleteEvent(
        session_id="s1",
        source="agent_loop",
        parent_session_id="s1",
        child_session_id="s2",
        child_outcome="success",
    )
    assert ev.event_type == "delegation_complete"
    assert ev.child_outcome == "success"


def test_memory_write_event_class_exists():
    from plugin_sdk.ingestion import MemoryWriteEvent

    ev = MemoryWriteEvent(
        session_id="s1",
        source="agent_memory",
        action="append",
        target="MEMORY.md",
        content_size=128,
    )
    assert ev.event_type == "memory_write"
    assert ev.action == "append"
    # Privacy: content_size, not content
    assert not hasattr(ev, "content")


def test_turn_start_event_no_content_field():
    """TurnStartEvent must not expose a 'content' field (privacy)."""
    from plugin_sdk.ingestion import TurnStartEvent

    ev = TurnStartEvent(session_id="s", source="x", turn_index=1)
    assert not hasattr(ev, "content")


def test_delegation_complete_event_outcomes():
    from plugin_sdk.ingestion import DelegationCompleteEvent

    for outcome in ("success", "failure", "error"):
        ev = DelegationCompleteEvent(
            session_id="s",
            source="x",
            parent_session_id="p",
            child_session_id="c",
            child_outcome=outcome,
        )
        assert ev.child_outcome == outcome


def test_memory_write_event_is_frozen():
    """MemoryWriteEvent is immutable (frozen dataclass)."""
    from plugin_sdk.ingestion import MemoryWriteEvent

    ev = MemoryWriteEvent(
        session_id="s", source="x",
        action="append", target="MEMORY.md", content_size=10,
    )
    with pytest.raises((AttributeError, TypeError)):
        ev.content_size = 99  # type: ignore[misc]


# ─── T3.2: MemoryProvider new hooks ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_provider_default_on_turn_start_is_noop():
    """Default no-op for backwards compat."""
    from plugin_sdk.memory import MemoryProvider

    class StubProvider(MemoryProvider):
        provider_id = "stub-pr8"
        provider_priority = 100

        def tool_schemas(self):
            return []

        async def handle_tool_call(self, call):
            return None

        async def prefetch(self, query, turn_index):
            return None

        async def sync_turn(self, user, assistant, turn_index):
            return None

        async def health_check(self):
            return True

    p = StubProvider()
    assert await p.on_turn_start(session_id="s", turn_index=1) is None
    assert await p.on_delegation(
        parent_session_id="p", child_session_id="c", child_outcome="success"
    ) is None
    assert await p.on_memory_write(
        action="append", target="MEMORY.md", content_size=10
    ) is None


@pytest.mark.asyncio
async def test_memory_provider_new_hooks_override():
    """Provider subclass can override on_turn_start / on_delegation / on_memory_write."""
    from plugin_sdk.memory import MemoryProvider

    calls = []

    class TrackingProvider(MemoryProvider):
        provider_id = "tracking-pr8"
        provider_priority = 100

        def tool_schemas(self):
            return []

        async def handle_tool_call(self, call):
            return None

        async def prefetch(self, query, turn_index):
            return None

        async def sync_turn(self, user, assistant, turn_index):
            return None

        async def health_check(self):
            return True

        async def on_turn_start(self, *, session_id, turn_index):
            calls.append(("turn_start", session_id, turn_index))

        async def on_delegation(self, *, parent_session_id, child_session_id, child_outcome):
            calls.append(("delegation", parent_session_id, child_session_id, child_outcome))

        async def on_memory_write(self, *, action, target, content_size):
            calls.append(("memory_write", action, target, content_size))

    p = TrackingProvider()
    await p.on_turn_start(session_id="s1", turn_index=2)
    await p.on_delegation(parent_session_id="p", child_session_id="c", child_outcome="success")
    await p.on_memory_write(action="append", target="MEMORY.md", content_size=100)

    assert calls == [
        ("turn_start", "s1", 2),
        ("delegation", "p", "c", "success"),
        ("memory_write", "append", "MEMORY.md", 100),
    ]


# ─── T3.2: MemoryBridge.register_with_bus ────────────────────────────────────


@pytest.mark.asyncio
async def test_register_with_bus_creates_subscriptions():
    """register_with_bus returns 3 subscription handles."""
    from opencomputer.agent.memory_bridge import MemoryBridge
    from opencomputer.ingestion.bus import TypedEventBus

    # Use a fresh isolated bus instead of resetting the singleton so we
    # don't leave the module-level default_bus in a replaced state
    # (which would break test_typed_event_bus.py::test_default_bus_is_singleton
    # when running the full suite).
    isolated_bus = TypedEventBus()

    # Build a minimal MemoryContext stub
    ctx = MagicMock()
    ctx.provider = None
    ctx._failure_state = {}
    bridge = MemoryBridge(ctx)
    subs = bridge.register_with_bus(bus=isolated_bus)
    assert len(subs) == 3
    # Each subscription has unsubscribe (bus.Subscription)
    for s in subs:
        assert hasattr(s, "unsubscribe") or hasattr(s, "cancel")


@pytest.mark.asyncio
async def test_register_with_bus_routes_turn_start_to_provider():
    """Publishing TurnStartEvent calls provider.on_turn_start via the bridge."""
    from opencomputer.agent.memory_bridge import MemoryBridge
    from opencomputer.ingestion.bus import TypedEventBus
    from plugin_sdk.ingestion import TurnStartEvent

    bus = TypedEventBus()  # isolated bus — does not affect the global singleton

    received = []

    async def fake_on_turn_start(*, session_id, turn_index):
        received.append((session_id, turn_index))

    provider = MagicMock()
    provider.on_turn_start = fake_on_turn_start

    ctx = MagicMock()
    ctx.provider = provider
    ctx._failure_state = {}

    bridge = MemoryBridge(ctx)
    # Override _is_disabled so provider is not short-circuited
    bridge._is_disabled = lambda: False

    bridge.register_with_bus(bus=bus)
    bus.publish(TurnStartEvent(session_id="sess-y", source="agent_loop", turn_index=5))

    # Sync publish + asyncio.run inside handler — give the event loop a beat
    import asyncio
    await asyncio.sleep(0)
    assert ("sess-y", 5) in received


@pytest.mark.asyncio
async def test_register_with_bus_routes_memory_write_to_provider():
    """Publishing MemoryWriteEvent calls provider.on_memory_write via the bridge."""
    from opencomputer.agent.memory_bridge import MemoryBridge
    from opencomputer.ingestion.bus import TypedEventBus
    from plugin_sdk.ingestion import MemoryWriteEvent

    bus = TypedEventBus()  # isolated bus — does not affect the global singleton

    received = []

    async def fake_on_memory_write(*, action, target, content_size):
        received.append((action, target, content_size))

    provider = MagicMock()
    provider.on_memory_write = fake_on_memory_write

    ctx = MagicMock()
    ctx.provider = provider
    ctx._failure_state = {}

    bridge = MemoryBridge(ctx)
    bridge._is_disabled = lambda: False

    bridge.register_with_bus(bus=bus)
    bus.publish(MemoryWriteEvent(
        session_id=None,
        source="agent_memory",
        action="append",
        target="MEMORY.md",
        content_size=512,
    ))

    import asyncio
    await asyncio.sleep(0)
    assert ("append", "MEMORY.md", 512) in received


# ─── T3.2: MemoryManager publishes MemoryWriteEvent ──────────────────────────


def test_memory_manager_append_publishes_memory_write_event(tmp_path, monkeypatch):
    """MemoryManager._append publishes a MemoryWriteEvent to the bus."""
    import opencomputer.agent.memory as _mem_module
    import opencomputer.ingestion.bus as _bus_module
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.ingestion.bus import TypedEventBus
    from plugin_sdk.ingestion import MemoryWriteEvent

    # Patch the default_bus used by _publish_memory_write_event without
    # touching the module-level singleton (preserves singleton test isolation).
    isolated_bus = TypedEventBus()
    monkeypatch.setattr(_bus_module, "default_bus", isolated_bus)

    captured = []
    isolated_bus.subscribe("memory_write", captured.append)

    mem = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=tmp_path / "skills",
    )
    mem.append_declarative("Hello from PR-8")

    assert len(captured) == 1
    ev = captured[0]
    assert isinstance(ev, MemoryWriteEvent)
    assert ev.action == "append"
    assert ev.target == "MEMORY.md"
    assert ev.content_size > 0
    assert not hasattr(ev, "content") or "content" not in dir(type(ev))


# ─── loop publishing placeholders ─────────────────────────────────────────────


@pytest.mark.skip(reason="TODO: heavy AgentLoop test harness needed for full loop publishing")
async def test_loop_publishes_turn_start_events():
    """AgentLoop publishes TurnStartEvent at the top of each iteration."""


@pytest.mark.skip(reason="TODO: DelegateTool.execute requires factory injection")
async def test_delegate_tool_publishes_delegation_complete():
    """DelegateTool.execute publishes DelegationCompleteEvent after subagent finishes."""
