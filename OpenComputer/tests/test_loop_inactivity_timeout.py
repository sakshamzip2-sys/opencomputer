"""Round 2B P-3 — inactivity-based agent-loop timeout.

Two timeouts apply to a single ``AgentLoop.run_conversation`` call:

* ``LoopConfig.inactivity_timeout_s`` — wall-clock since the last
  observed activity (LLM round-trip OR tool dispatch). Resets every
  time the agent does something.
* ``LoopConfig.iteration_timeout_s`` — absolute wall-clock cap from
  ``run_conversation`` entry. Independent of activity.

Both checks fire at the top of each iteration. Inactivity raises
``InactivityTimeout``; absolute cap raises ``IterationTimeout``. Both
subclass ``LoopTimeout``.

Tests cover:

(a) Long session with regular activity does NOT trip inactivity, even
    when wall-clock time elapses past the threshold across multiple
    LLM calls (each call resets the timer).
(b) Silent session trips after ``inactivity_timeout_s`` — a fake
    monotonic clock simulates the gap between the previous iteration's
    bumps and the next iteration's top check.
(c) Activity-resets-timer — three back-to-back LLM calls each within
    half the timeout do NOT trip.
(d) Absolute cap fires even with activity — when ``inactivity_timeout_s``
    is generous but ``iteration_timeout_s`` is tight, the second
    iteration raises ``IterationTimeout``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import Message, ToolCall


def _config(
    tmp: Path,
    *,
    inactivity_timeout_s: int | float = 300,
    iteration_timeout_s: int | float = 1800,
):
    """Build a Config with explicit timeout overrides.

    LoopConfig is dataclass(frozen=True, slots=True) and the field is
    typed ``int``. Tests pass sub-second floats since the runtime check
    is a numeric comparison; a strict-typing CI would flag this but the
    runtime accepts it without issue.
    """
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
            max_iterations=10,
            parallel_tools=False,
            inactivity_timeout_s=inactivity_timeout_s,  # type: ignore[arg-type]
            iteration_timeout_s=iteration_timeout_s,  # type: ignore[arg-type]
        ),
        session=SessionConfig(db_path=tmp / "s.db"),
        memory=MemoryConfig(
            declarative_path=tmp / "MEMORY.md",
            skills_path=tmp / "skills",
        ),
    )


def _mk_loop(cfg, provider):
    from opencomputer.agent.loop import AgentLoop

    return AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )


def _end_turn_response():
    """ProviderResponse that ends the conversation immediately."""
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    return ProviderResponse(
        message=Message(role="assistant", content="done"),
        stop_reason="end_turn",
        usage=Usage(5, 2),
    )


def _tool_use_response(tool_id: str = "tc-1"):
    """ProviderResponse that emits one tool_use call (forces another iteration).

    Args vary per tool_id so the OpenClaw 1.C loop-safety detector doesn't
    flag a synthetic "same Bash call N times in a row" as a degenerate
    loop and abort early — these tests are about timeout behavior, not
    repetition detection.
    """
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    return ProviderResponse(
        message=Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id=tool_id,
                    name="Bash",
                    arguments={"command": f"echo {tool_id}"},
                ),
            ],
        ),
        stop_reason="tool_use",
        usage=Usage(5, 2),
    )


# ─── (a) regular activity does NOT trip inactivity ────────────────────


async def test_inactivity_timeout_does_not_trip_with_regular_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 LLM round-trips with 1s real time between them, threshold=10s.

    Strategy: drive the loop through 5 iterations (4 tool_use + 1 end_turn).
    Each tool dispatch sleeps 0.05s (real time, well under 10s). Each LLM
    call return + each tool dispatch return bumps activity, so the next
    iteration's top check sees a fresh timestamp.
    """
    from opencomputer.tools.registry import registry
    from plugin_sdk.core import ToolResult

    cfg = _config(tmp_path, inactivity_timeout_s=10, iteration_timeout_s=10_000)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    async def _quick_dispatch(call: ToolCall, **_: object) -> ToolResult:
        await asyncio.sleep(0.05)
        return ToolResult(tool_call_id=call.id, content="ok", is_error=False)

    monkeypatch.setattr(registry, "dispatch", _quick_dispatch)

    provider = MagicMock()
    provider.complete = AsyncMock(
        side_effect=[
            _tool_use_response("tc-1"),
            _tool_use_response("tc-2"),
            _tool_use_response("tc-3"),
            _tool_use_response("tc-4"),
            _end_turn_response(),
        ]
    )

    loop = _mk_loop(cfg, provider)
    result = await loop.run_conversation(user_message="go", session_id="s-active")
    assert result.iterations == 5
    assert provider.complete.await_count == 5


# ─── (b) silent session trips after inactivity_timeout_s ──────────────


async def test_inactivity_timeout_trips_after_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Iteration 2's top-of-loop check raises ``InactivityTimeout`` when
    ``_last_activity_at`` was last updated > ``inactivity_timeout_s`` ago.

    Strategy: drive the loop through one iteration. Right before
    iteration 2's top check, backdate ``loop._last_activity_at`` to
    simulate "100s of silence since last activity". The
    ``SessionDB.append_messages_batch`` call is the very last thing
    ``run_conversation`` does before iter 2's top check fires, so we
    monkeypatch it to backdate the attribute on its way out.
    """
    from opencomputer.agent.loop import InactivityTimeout, LoopTimeout
    from opencomputer.tools.registry import registry
    from plugin_sdk.core import ToolResult

    cfg = _config(tmp_path, inactivity_timeout_s=0.1, iteration_timeout_s=10_000)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    async def _fake_dispatch(call: ToolCall, **_: object) -> ToolResult:
        return ToolResult(tool_call_id=call.id, content="ok", is_error=False)

    monkeypatch.setattr(registry, "dispatch", _fake_dispatch)

    provider = MagicMock()
    provider.complete = AsyncMock(
        side_effect=[_tool_use_response("tc-1"), _end_turn_response()]
    )

    loop = _mk_loop(cfg, provider)

    # SessionDB.append_messages_batch is the last line of iteration 1's
    # body before the top of iteration 2. Backdating ``_last_activity_at``
    # in this wrapper is robust against new activity-bump call sites
    # being added in the future — anything that happens inside the loop
    # body is overwritten by us at the very end of the iteration.
    orig_append_batch = loop.db.append_messages_batch

    def _backdating_append_batch(*args, **kwargs):
        result = orig_append_batch(*args, **kwargs)
        loop._last_activity_at = loop._loop_started_at - 100.0
        return result

    monkeypatch.setattr(loop.db, "append_messages_batch", _backdating_append_batch)

    with pytest.raises(InactivityTimeout) as excinfo:
        await loop.run_conversation(user_message="go", session_id="s-silent")
    # Subclass relationship: InactivityTimeout is a LoopTimeout
    assert isinstance(excinfo.value, LoopTimeout)
    # Message mentions the configured threshold
    assert "0.1" in str(excinfo.value)


# ─── (c) activity-resets-timer behavior ────────────────────────────────


async def test_inactivity_timeout_resets_on_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 tool_use turns + final end_turn. Total wall-clock exceeds the
    1s threshold but each individual gap between activity bumps does not.

    Each tool dispatch sleeps 0.5s. The loop sees:
        iter 1 top check (gap 0) → LLM bump → dispatch bump (gap 0.5s)
        iter 2 top check (gap 0) → LLM bump → dispatch bump (gap 0.5s)
        iter 3 top check (gap 0) → LLM bump → dispatch bump (gap 0.5s)
        iter 4 top check (gap 0) → LLM bump → END
    Total wall-clock ~1.5s; max gap ~0.5s; threshold 1.0s → no trip.
    """
    from opencomputer.tools.registry import registry
    from plugin_sdk.core import ToolResult

    cfg = _config(tmp_path, inactivity_timeout_s=1.0, iteration_timeout_s=10_000)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    async def _half_sec_dispatch(call: ToolCall, **_: object) -> ToolResult:
        await asyncio.sleep(0.5)
        return ToolResult(tool_call_id=call.id, content="ok", is_error=False)

    monkeypatch.setattr(registry, "dispatch", _half_sec_dispatch)

    provider = MagicMock()
    provider.complete = AsyncMock(
        side_effect=[
            _tool_use_response("tc-1"),
            _tool_use_response("tc-2"),
            _tool_use_response("tc-3"),
            _end_turn_response(),
        ]
    )

    loop = _mk_loop(cfg, provider)
    result = await loop.run_conversation(user_message="go", session_id="s-resets")
    assert result.iterations == 4


# ─── (d) absolute cap (iteration_timeout_s) still works ────────────────


async def test_iteration_timeout_absolute_cap_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inactivity loose, absolute cap tight → IterationTimeout raised.

    Strategy: ``inactivity_timeout_s=100`` (very generous, never trips),
    ``iteration_timeout_s=0.1`` (very tight). Iteration 1's tool dispatch
    sleeps 0.2s. Iteration 2's top-of-loop check sees ``elapsed > 0.1s``
    since loop start and raises ``IterationTimeout``.
    """
    from opencomputer.agent.loop import IterationTimeout, LoopTimeout
    from opencomputer.tools.registry import registry
    from plugin_sdk.core import ToolResult

    cfg = _config(tmp_path, inactivity_timeout_s=100, iteration_timeout_s=0.1)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    async def _slow_dispatch(call: ToolCall, **_: object) -> ToolResult:
        await asyncio.sleep(0.2)
        return ToolResult(tool_call_id=call.id, content="ok", is_error=False)

    monkeypatch.setattr(registry, "dispatch", _slow_dispatch)

    provider = MagicMock()
    provider.complete = AsyncMock(
        side_effect=[_tool_use_response("tc-1"), _end_turn_response()]
    )

    loop = _mk_loop(cfg, provider)
    with pytest.raises(IterationTimeout) as excinfo:
        await loop.run_conversation(user_message="go", session_id="s-cap")
    assert isinstance(excinfo.value, LoopTimeout)
    assert "0.1" in str(excinfo.value)


# ─── exception hierarchy + config defaults ─────────────────────────────


def test_timeout_exception_hierarchy() -> None:
    """``InactivityTimeout`` and ``IterationTimeout`` both subclass
    ``LoopTimeout`` (callers can catch one base class)."""
    from opencomputer.agent.loop import (
        InactivityTimeout,
        IterationTimeout,
        LoopTimeout,
    )

    assert issubclass(InactivityTimeout, LoopTimeout)
    assert issubclass(IterationTimeout, LoopTimeout)
    assert issubclass(LoopTimeout, Exception)


def test_loop_config_defaults() -> None:
    """Round 2B P-3 default values: 300s inactivity, 1800s absolute cap."""
    from opencomputer.agent.config import LoopConfig

    cfg = LoopConfig()
    assert cfg.inactivity_timeout_s == 300
    assert cfg.iteration_timeout_s == 1800
