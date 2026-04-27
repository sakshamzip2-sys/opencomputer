"""tests/test_skill_evolution_sdk.py — T1 of auto-skill-evolution plan.

Pins three contracts:

1. ``SessionEndEvent`` exists in :mod:`plugin_sdk.ingestion`, inherits
   :class:`SignalEvent`, and is frozen with safe defaults.
2. Three new F1 capabilities — ``skill_evolution.observe`` /
   ``.propose`` / ``.auto_publish`` — are registered in
   :data:`opencomputer.agent.consent.capability_taxonomy.F1_CAPABILITIES`
   at the correct consent tiers.
3. The agent loop publishes a ``SessionEndEvent`` at the END_TURN
   terminal point. The event carries a positive turn count and a
   non-negative wall-clock duration; ``had_errors`` is False when no
   tool returned an error.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.consent.capability_taxonomy import F1_CAPABILITIES
from plugin_sdk.consent import ConsentTier
from plugin_sdk.core import Message
from plugin_sdk.ingestion import SessionEndEvent, SignalEvent

# ─── 1. SessionEndEvent contract ─────────────────────────────────────


def test_session_end_event_inherits_signal_event() -> None:
    e = SessionEndEvent(end_reason="completed", turn_count=5, duration_seconds=12.5)
    assert isinstance(e, SignalEvent)
    assert e.event_type == "session_end"
    assert e.turn_count == 5
    assert e.duration_seconds == 12.5


def test_session_end_event_default_fields_safe() -> None:
    e = SessionEndEvent()
    assert e.end_reason == "completed"
    assert e.turn_count == 0
    assert e.duration_seconds == 0.0
    assert e.had_errors is False


def test_session_end_event_is_frozen() -> None:
    e = SessionEndEvent()
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.turn_count = 5  # type: ignore[misc]


def test_session_end_event_in_module_all() -> None:
    """SessionEndEvent must be in ``__all__`` so re-exports stay clean."""
    from plugin_sdk import ingestion as ingestion_mod

    assert "SessionEndEvent" in ingestion_mod.__all__


# ─── 2. F1 capability registration ───────────────────────────────────


def test_skill_evolution_capabilities_registered() -> None:
    assert F1_CAPABILITIES.get("skill_evolution.observe") == ConsentTier.IMPLICIT
    assert F1_CAPABILITIES.get("skill_evolution.propose") == ConsentTier.EXPLICIT
    assert F1_CAPABILITIES.get("skill_evolution.auto_publish") == ConsentTier.PER_ACTION


def test_capability_namespaces_use_dot_separator() -> None:
    for k in (
        "skill_evolution.observe",
        "skill_evolution.propose",
        "skill_evolution.auto_publish",
    ):
        assert k in F1_CAPABILITIES
        assert "/" not in k and ":" not in k


# ─── 3. Loop emits SessionEndEvent on END_TURN ───────────────────────


def _config(tmp: Path):
    """Minimal Config for an in-process agent loop."""
    from opencomputer.agent.config import (
        Config,
        LoopConfig,
        MemoryConfig,
        ModelConfig,
        SessionConfig,
    )

    return Config(
        model=ModelConfig(
            provider="mock",
            model="main-model",
            max_tokens=512,
            temperature=0.0,
        ),
        loop=LoopConfig(
            max_iterations=2,
            parallel_tools=False,
        ),
        session=SessionConfig(db_path=tmp / "s.db"),
        memory=MemoryConfig(
            declarative_path=tmp / "MEMORY.md",
            skills_path=tmp / "skills",
        ),
    )


def _end_turn_response():
    """ProviderResponse that ends the conversation immediately."""
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    return ProviderResponse(
        message=Message(role="assistant", content="done"),
        stop_reason="end_turn",
        usage=Usage(5, 2),
    )


@pytest.fixture
def captured_session_end_bus(monkeypatch):
    """Swap default_bus for a fresh instance + collect session_end events."""
    from opencomputer.ingestion import bus as bus_module

    fresh = bus_module.TypedEventBus()
    monkeypatch.setattr(bus_module, "default_bus", fresh, raising=True)

    captured: list[SessionEndEvent] = []

    async def _async_collector(evt):
        captured.append(evt)

    fresh.subscribe("session_end", _async_collector)
    return fresh, captured


async def test_session_end_event_emitted_on_loop_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_session_end_bus,
) -> None:
    """A successful END_TURN run publishes one SessionEndEvent.

    The event must carry the correct session_id, source="agent_loop",
    turn_count >= 1 (one LLM round-trip happened), a non-negative
    duration, and had_errors=False (no tool calls were made).
    """
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.tools.registry import registry

    _bus, captured = captured_session_end_bus

    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=[_end_turn_response()])

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )

    result = await loop.run_conversation(
        user_message="hi", session_id="sess-end-1"
    )
    # Provider was called exactly once (no tool loop, immediate END_TURN).
    assert provider.complete.await_count == 1
    assert result.iterations == 1

    # Allow asyncio scheduling the bus subscriber to run if needed.
    await asyncio.sleep(0)

    assert len(captured) == 1, f"expected 1 SessionEndEvent, got {len(captured)}"
    evt = captured[0]
    assert isinstance(evt, SessionEndEvent)
    assert evt.session_id == "sess-end-1"
    assert evt.source == "agent_loop"
    assert evt.end_reason == "completed"
    assert evt.turn_count >= 1
    assert evt.duration_seconds >= 0.0
    assert evt.had_errors is False


async def test_session_end_event_emitted_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_session_end_bus,
) -> None:
    """A loop that raises ``IterationTimeout`` still emits a SessionEndEvent.

    Emission MUST happen on every terminal path including exceptions —
    that's the contract the evolution subscriber relies on.
    """
    from opencomputer.agent.loop import AgentLoop, IterationTimeout
    from opencomputer.tools.registry import registry
    from plugin_sdk.core import ToolCall, ToolResult
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    _bus, captured = captured_session_end_bus

    cfg = _config(tmp_path)
    # Tight wall-clock cap so iteration 2 trips immediately after a slow tool.
    cfg = dataclasses.replace(
        cfg,
        loop=dataclasses.replace(
            cfg.loop, inactivity_timeout_s=100, iteration_timeout_s=0.05
        ),
    )
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    async def _slow_dispatch(call: ToolCall, **_: object) -> ToolResult:
        await asyncio.sleep(0.15)
        return ToolResult(tool_call_id=call.id, content="ok", is_error=False)

    monkeypatch.setattr(registry, "dispatch", _slow_dispatch)

    def _tool_use_response():
        return ProviderResponse(
            message=Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="tc-1", name="Bash", arguments={"command": "echo"}),
                ],
            ),
            stop_reason="tool_use",
            usage=Usage(5, 2),
        )

    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=[_tool_use_response()])

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )

    with pytest.raises(IterationTimeout):
        await loop.run_conversation(
            user_message="go", session_id="sess-end-timeout"
        )

    await asyncio.sleep(0)
    assert len(captured) == 1
    evt = captured[0]
    assert evt.session_id == "sess-end-timeout"
    assert evt.end_reason == "timeout"
    assert evt.had_errors is True
    assert evt.duration_seconds >= 0.0


async def test_loop_does_not_break_when_session_end_publish_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bus that raises on apublish must NOT break run_conversation's return."""
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.ingestion import bus as bus_module
    from opencomputer.tools.registry import registry

    class _BrokenBus(bus_module.TypedEventBus):
        async def apublish(self, event):  # type: ignore[override]
            raise RuntimeError("simulated bus outage")

    monkeypatch.setattr(bus_module, "default_bus", _BrokenBus(), raising=True)

    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=[_end_turn_response()])

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )

    # Despite the broken bus, run_conversation should return cleanly.
    result = await loop.run_conversation(
        user_message="hi", session_id="sess-end-broken-bus"
    )
    assert result.iterations == 1
    assert result.session_id == "sess-end-broken-bus"
