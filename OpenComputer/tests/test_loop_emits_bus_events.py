"""
Phase 3.A — the agent loop publishes :class:`ToolCallEvent` after each
tool invocation.

This is the only publisher wired in by Phase 3.A; later phases add
more. The invariants being pinned here (exception isolation, outcome
mapping) protect Session B's B3 subscriber — if the loop stops
emitting (or emits the wrong outcome), B3 silently misses data.

Test strategy
-------------

All tests build a real :class:`AgentLoop` against a mock provider,
swap the module-level ``default_bus`` for a fresh :class:`TypedEventBus`
(via ``opencomputer.ingestion.bus.default_bus`` monkeypatch), and
subscribe an in-process collector. We drive ``_dispatch_tool_calls``
directly rather than through the full ``run_conversation`` path so
the assertions stay targeted at the emission point.
"""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.agent.config import Config, LoopConfig
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.state import SessionDB
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import Message, ToolCall, ToolResult
from plugin_sdk.ingestion import SignalEvent, ToolCallEvent
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage
from plugin_sdk.tool_contract import BaseTool, ToolSchema

# ─── shared helpers ────────────────────────────────────────────────


class _RecordingTool(BaseTool):
    """Tool that succeeds and records a count."""

    def __init__(self, name: str = "Ping") -> None:
        self._name = name
        self.calls = 0

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self._name,
            description="test",
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls += 1
        return ToolResult(tool_call_id=call.id, content="ok")


class _FailingTool(BaseTool):
    """Tool whose dispatcher returns an is_error=True result.

    Simulates "tool ran but returned an error" — the existing
    registry.dispatch path converts uncaught exceptions into
    is_error=True responses, so this models the realistic failure
    observed by the loop.
    """

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Boom",
            description="always fails",
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(tool_call_id=call.id, content="nope", is_error=True)


class _EndTurnProvider(BaseProvider):
    """Provider that never calls tools — a bare ``end_turn``."""

    async def complete(self, *, model, messages, system, tools, max_tokens, temperature):
        return ProviderResponse(
            message=Message(role="assistant", content="done"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    async def stream_complete(self, *, model, messages, system, tools, max_tokens, temperature):
        resp = await self.complete(
            model=model, messages=messages, system=system, tools=tools,
            max_tokens=max_tokens, temperature=temperature,
        )

        class _Done:
            kind = "done"

            def __init__(self, r):
                self.response = r

        yield _Done(resp)


def _make_loop(tmp_path) -> AgentLoop:
    """Minimal loop wired to tmp SessionDB, no reviewer/episodic/compaction."""
    cfg = Config(
        loop=LoopConfig(max_iterations=1, parallel_tools=False),
        session=type(Config().session)(db_path=tmp_path / "s.db"),  # type: ignore[call-arg]
    )
    return AgentLoop(
        provider=_EndTurnProvider(),
        config=cfg,
        db=SessionDB(tmp_path / "s.db"),
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )


@pytest.fixture
def captured_bus(monkeypatch):
    """Fresh bus swapped in for the duration of one test.

    Replaces the module-level ``default_bus`` attribute so the
    loop's ``_emit_tool_call_event`` picks up our clean instance.
    Restores on teardown.
    """
    from opencomputer.ingestion import bus as bus_module

    fresh = bus_module.TypedEventBus()
    monkeypatch.setattr(bus_module, "default_bus", fresh, raising=True)
    captured: list[SignalEvent] = []
    fresh.subscribe("tool_call", captured.append)
    return fresh, captured


@pytest.fixture
def isolated_registry(monkeypatch):
    """Swap the loop module's registry for a fresh ToolRegistry per test."""
    import opencomputer.agent.loop as loop_mod

    real_registry = loop_mod.registry
    test_reg = ToolRegistry()
    monkeypatch.setattr(loop_mod, "registry", test_reg, raising=True)
    yield test_reg
    loop_mod.registry = real_registry


# ─── 1. Successful tool call → success event ────────────────────────


def test_successful_tool_call_emits_tool_call_event(
    tmp_path, captured_bus, isolated_registry,
) -> None:
    """After a successful tool call, ToolCallEvent(outcome='success') is published."""
    _bus, captured = captured_bus
    tool = _RecordingTool(name="Ping")
    isolated_registry.register(tool)

    loop = _make_loop(tmp_path)
    calls = [ToolCall(id="c1", name="Ping", arguments={"x": 1})]

    asyncio.run(
        loop._dispatch_tool_calls(calls, session_id="sess-1", turn_index=0)
    )

    assert tool.calls == 1
    assert len(captured) == 1
    evt = captured[0]
    assert isinstance(evt, ToolCallEvent)
    assert evt.tool_name == "Ping"
    assert evt.outcome == "success"
    assert evt.session_id == "sess-1"
    assert evt.source == "agent_loop"
    # Arguments preserved (copy — so mutating the original doesn't mutate the event).
    assert evt.arguments == {"x": 1}
    assert evt.duration_seconds >= 0.0


# ─── 2. Tool returning is_error=True → failure event ────────────────


def test_failed_tool_call_emits_event_with_failure_outcome(
    tmp_path, captured_bus, isolated_registry,
) -> None:
    """Tool whose result has is_error=True publishes outcome='failure'."""
    _bus, captured = captured_bus
    isolated_registry.register(_FailingTool())

    loop = _make_loop(tmp_path)
    calls = [ToolCall(id="c1", name="Boom", arguments={})]

    asyncio.run(
        loop._dispatch_tool_calls(calls, session_id="sess-2", turn_index=0)
    )

    assert len(captured) == 1
    evt = captured[0]
    assert isinstance(evt, ToolCallEvent)
    assert evt.outcome == "failure"
    assert evt.tool_name == "Boom"


# ─── 3. Blocked-by-hook → blocked event ─────────────────────────────


def test_blocked_tool_call_emits_event_with_blocked_outcome(
    tmp_path, captured_bus, isolated_registry,
) -> None:
    """When a PreToolUse hook blocks the call, a 'blocked' event is emitted."""
    from opencomputer.hooks.engine import engine as hook_engine
    from plugin_sdk.hooks import HookDecision, HookEvent, HookSpec

    _bus, captured = captured_bus
    isolated_registry.register(_RecordingTool(name="Ping"))

    async def blocker(ctx):
        return HookDecision(decision="block", reason="test block")

    spec = HookSpec(
        event=HookEvent.PRE_TOOL_USE,
        handler=blocker,
        fire_and_forget=False,
    )
    hook_engine.register(spec)
    try:
        loop = _make_loop(tmp_path)
        calls = [ToolCall(id="c1", name="Ping", arguments={})]
        asyncio.run(
            loop._dispatch_tool_calls(calls, session_id="sess-3", turn_index=0)
        )
    finally:
        hook_engine.unregister_all(HookEvent.PRE_TOOL_USE)

    assert len(captured) == 1
    assert captured[0].outcome == "blocked"


# ─── 4. Bus publish failure must NOT break the loop ─────────────────


def test_loop_does_not_break_when_bus_publish_raises(
    tmp_path, monkeypatch, isolated_registry, caplog,
) -> None:
    """A broken bus publish is swallowed at WARNING — dispatch still succeeds."""
    import logging

    from opencomputer.ingestion import bus as bus_module

    class _BrokenBus(bus_module.TypedEventBus):
        def publish(self, event):
            raise RuntimeError("simulated bus outage")

    monkeypatch.setattr(bus_module, "default_bus", _BrokenBus(), raising=True)

    tool = _RecordingTool(name="Ping")
    isolated_registry.register(tool)

    loop = _make_loop(tmp_path)
    calls = [ToolCall(id="c1", name="Ping", arguments={})]

    with caplog.at_level(logging.WARNING, logger="opencomputer.agent.loop"):
        results = asyncio.run(
            loop._dispatch_tool_calls(calls, session_id="s", turn_index=0)
        )

    # The tool itself still ran.
    assert tool.calls == 1
    assert len(results) == 1
    # A warning was logged.
    assert any("bus" in r.getMessage().lower() for r in caplog.records)


# ─── 5. Parallel-safe batch emits one event per call ────────────────


def test_parallel_batch_emits_one_event_per_tool_call(
    tmp_path, captured_bus, isolated_registry,
) -> None:
    """N parallel calls → N events, in any order but matching count."""
    _bus, captured = captured_bus
    tool = _RecordingTool(name="Ping")
    isolated_registry.register(tool)

    # Force sequential to keep the test deterministic — the invariant
    # we're asserting is the count, not ordering.
    loop = _make_loop(tmp_path)
    calls = [
        ToolCall(id=f"c{i}", name="Ping", arguments={"i": i}) for i in range(3)
    ]

    asyncio.run(
        loop._dispatch_tool_calls(calls, session_id="s", turn_index=0)
    )

    assert len(captured) == 3
    # Each event carries the correct (copied) argument map.
    i_values = sorted(e.arguments["i"] for e in captured)  # type: ignore[attr-defined]
    assert i_values == [0, 1, 2]
