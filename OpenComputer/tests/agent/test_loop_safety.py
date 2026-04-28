"""Tests for the LoopDetector + LoopAbortError.

Per AMENDMENTS H5 fix, the detector is scoped per ``(session_id,
delegation_depth)``. Each delegated subagent owns its own frame so a
parent's repetition history can't poison a child's window (and vice versa).
"""
from __future__ import annotations

import pytest

# ─── single-frame happy paths ─────────────────────────────────────────


def test_detector_flags_third_repeat_tool_call():
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector(max_tool_repeats=3, max_text_repeats=2, window_size=10)
    d.push_frame("s1", 0)

    for _ in range(2):
        d.record_tool_call("s1", 0, "Bash", "h-1")
        assert not d.flagged("s1", 0)

    d.record_tool_call("s1", 0, "Bash", "h-1")
    assert d.flagged("s1", 0)
    assert "Bash" in d.warning("s1", 0)
    assert not d.must_stop("s1", 0)


def test_detector_must_stop_after_consecutive_flags():
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector(
        max_tool_repeats=2,
        max_text_repeats=2,
        window_size=10,
        max_consecutive_flags=2,
    )
    d.push_frame("s1", 0)

    d.record_tool_call("s1", 0, "Bash", "h-1")
    d.record_tool_call("s1", 0, "Bash", "h-1")
    assert d.flagged("s1", 0)
    # 2nd consecutive flag → must_stop trips
    d.record_tool_call("s1", 0, "Bash", "h-1")
    assert d.must_stop("s1", 0)


def test_detector_resets_when_unique_tool_call():
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector(max_tool_repeats=2, max_text_repeats=2)
    d.push_frame("s1", 0)

    d.record_tool_call("s1", 0, "Bash", "h-1")
    d.record_tool_call("s1", 0, "Bash", "h-1")
    assert d.flagged("s1", 0)

    # A unique call clears the flag and the consecutive-flag counter.
    d.record_tool_call("s1", 0, "Read", "h-2")
    assert not d.flagged("s1", 0)
    assert not d.must_stop("s1", 0)


def test_detector_text_repetition_flags_at_second_occurrence():
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector(max_tool_repeats=10, max_text_repeats=2)
    d.push_frame("s1", 0)

    d.record_assistant_text("s1", 0, "h-X")
    assert not d.flagged("s1", 0)
    d.record_assistant_text("s1", 0, "h-X")
    assert d.flagged("s1", 0)


def test_detector_window_bounds_memory():
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector(max_tool_repeats=3, max_text_repeats=2, window_size=5)
    d.push_frame("s1", 0)
    for i in range(20):
        d.record_tool_call("s1", 0, "X", str(i))

    # Internal sliding window for this frame never exceeds window_size.
    frame = d._frames[("s1", 0)]
    assert len(frame.tool_window) == 5


# ─── per-(session, depth) frame isolation (AMENDMENTS H5 fix) ─────────


def test_frames_are_isolated_per_session_depth():
    """Repetition in one frame doesn't leak into another frame."""
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector(max_tool_repeats=2, max_text_repeats=2)
    d.push_frame("s1", 0)
    d.push_frame("s1", 1)  # delegated subagent

    # Repeat in the parent frame.
    d.record_tool_call("s1", 0, "Bash", "h-1")
    d.record_tool_call("s1", 0, "Bash", "h-1")
    assert d.flagged("s1", 0)

    # Child frame is untouched.
    assert not d.flagged("s1", 1)

    # And vice-versa: a repeat in the child must not flag the parent.
    d.reset_frame("s1", 0)
    d.record_tool_call("s1", 1, "Read", "h-2")
    d.record_tool_call("s1", 1, "Read", "h-2")
    assert d.flagged("s1", 1)
    assert not d.flagged("s1", 0)


def test_frames_are_isolated_per_session():
    """Two top-level sessions don't share state either."""
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector(max_tool_repeats=2, max_text_repeats=2)
    d.push_frame("s1", 0)
    d.push_frame("s2", 0)

    d.record_tool_call("s1", 0, "Bash", "h-1")
    d.record_tool_call("s1", 0, "Bash", "h-1")
    assert d.flagged("s1", 0)
    assert not d.flagged("s2", 0)


def test_pop_frame_removes_state_cleanly():
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector(max_tool_repeats=2, max_text_repeats=2)
    d.push_frame("s1", 0)
    d.record_tool_call("s1", 0, "Bash", "h-1")
    d.record_tool_call("s1", 0, "Bash", "h-1")
    assert d.flagged("s1", 0)

    d.pop_frame("s1", 0)
    assert ("s1", 0) not in d._frames

    # Pushing the same key again starts fresh.
    d.push_frame("s1", 0)
    assert not d.flagged("s1", 0)


def test_reset_frame_clears_state_but_keeps_frame():
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector(max_tool_repeats=2, max_text_repeats=2)
    d.push_frame("s1", 0)
    d.record_tool_call("s1", 0, "Bash", "h-1")
    d.record_tool_call("s1", 0, "Bash", "h-1")
    assert d.flagged("s1", 0)

    d.reset_frame("s1", 0)
    assert ("s1", 0) in d._frames
    assert not d.flagged("s1", 0)
    assert not d.must_stop("s1", 0)


def test_methods_on_unknown_frame_are_safe_no_ops():
    """A frame that was never pushed (or already popped) must not crash.

    Treat the absent frame as 'no repetition seen' — flagged/must_stop
    return False, warning returns "", and record_* is a no-op. This
    keeps the agent loop's wiring tolerant of edge cases (e.g. an
    early exception path that pops before recording).
    """
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector()
    assert not d.flagged("missing", 0)
    assert not d.must_stop("missing", 0)
    assert d.warning("missing", 0) == ""
    # No-op — must not raise.
    d.record_tool_call("missing", 0, "Bash", "h-1")
    d.record_assistant_text("missing", 0, "h-X")
    d.reset_frame("missing", 0)
    d.pop_frame("missing", 0)


def test_loop_abort_error_is_subclass_of_runtime_error():
    from opencomputer.agent.loop_safety import LoopAbortError

    assert issubclass(LoopAbortError, RuntimeError)


# ─── integration: the agent loop wiring (Task C2) ─────────────────────


@pytest.mark.asyncio
async def test_agent_loop_aborts_on_repeated_identical_tool_calls(tmp_path):
    """Synthetic: feed AgentLoop a provider that always asks for Bash(ls).

    The detector with shortened thresholds (max_tool_repeats=2,
    max_consecutive_flags=2) flips to ``must_stop`` after the second-and-
    third identical tool call inside the same window. The loop must
    surface a ``LoopAbortError`` and convert it to a clean
    ``"Agent loop stopped: ..."`` final message rather than letting the
    model spin to ``max_iterations``.
    """
    from opencomputer.agent.config import (
        Config,
        LoopConfig,
        MemoryConfig,
        ModelConfig,
        SessionConfig,
    )
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.tools.registry import registry as _registry
    from plugin_sdk.core import Message, ToolCall, ToolResult
    from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage
    from plugin_sdk.tool_contract import BaseTool, ToolSchema

    # Provider stub: every call asks the agent to run a tool with the
    # exact same args, so args_hash is constant across iterations.
    class _StubProvider(BaseProvider):
        def __init__(self) -> None:
            self.complete_calls = 0
            self.name = "stub"

        async def complete(self, **_kwargs):
            self.complete_calls += 1
            tc = ToolCall(
                id=f"call-{self.complete_calls}",
                name="_LoopSafetyStubTool",
                arguments={"command": "ls"},
            )
            return ProviderResponse(
                message=Message(role="assistant", content="", tool_calls=[tc]),
                stop_reason="tool_use",
                usage=Usage(input_tokens=1, output_tokens=1),
            )

        async def stream_complete(self, **_kwargs):  # pragma: no cover
            raise NotImplementedError

    # Tool stub: returns a constant successful result. Registered under a
    # unique name so we don't collide with the real Bash tool that other
    # tests / live loop relies on.
    class _StubTool(BaseTool):
        @property
        def schema(self) -> ToolSchema:
            return ToolSchema(
                name="_LoopSafetyStubTool",
                description="loop-safety integration test stub",
                parameters={"type": "object", "properties": {}},
            )

        async def execute(self, call: ToolCall) -> ToolResult:
            return ToolResult(
                tool_call_id=call.id,
                content="ok",
                is_error=False,
            )

    _registry.unregister("_LoopSafetyStubTool")  # idempotent
    _registry.register(_StubTool())
    try:
        cfg = Config(
            model=ModelConfig(provider="stub", model="stub-model"),
            loop=LoopConfig(max_iterations=20),
            session=SessionConfig(db_path=tmp_path / "sessions.db"),
            memory=MemoryConfig(
                declarative_path=tmp_path / "MEMORY.md",
                user_path=tmp_path / "USER.md",
                skills_path=tmp_path / "skills",
                soul_path=tmp_path / "SOUL.md",
            ),
        )

        loop = AgentLoop(provider=_StubProvider(), config=cfg)
        # Tighten thresholds so the abort fires quickly and deterministically.
        loop._loop_detector.max_tool_repeats = 2
        loop._loop_detector.max_consecutive_flags = 2

        result = await loop.run_conversation(
            user_message="please list files",
            session_id="loop-safety-integration-1",
        )
    finally:
        _registry.unregister("_LoopSafetyStubTool")

    assert "Agent loop stopped" in (result.final_message.content or ""), (
        f"expected loop-aborted final message, got: "
        f"{result.final_message.content!r}"
    )
    # The loop must terminate well before the iteration budget runs out —
    # otherwise the detector isn't doing its job.
    assert result.iterations < 20
