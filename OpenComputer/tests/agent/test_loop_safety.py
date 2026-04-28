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
async def test_agent_loop_aborts_on_repeated_identical_tool_calls(monkeypatch, tmp_path):
    """Synthetic: feed AgentLoop a provider that always asks for Bash(ls).

    The detector defaults are 3 tool repeats + 2 consecutive flags, so
    after enough identical Bash(ls) calls the loop must surface a
    LoopAbortError-derived final message rather than letting the model
    spin forever.
    """
    from plugin_sdk.core import (
        Message,
        ProviderResponse,
        ToolCall,
        Usage,
    )
    from plugin_sdk.provider_contract import BaseProvider

    from opencomputer.agent.config import Config, LoopConfig, ModelConfig, MemoryConfig, SessionConfig
    from opencomputer.agent.loop import AgentLoop

    # Provider stub: every call asks the agent to run Bash(ls) again.
    class StubProvider(BaseProvider):
        def __init__(self):
            self._counter = 0

        async def complete(self, *, model, messages, system, tools, max_tokens, temperature):
            self._counter += 1
            tc = ToolCall(
                id=f"call-{self._counter}",
                name="Bash",
                arguments={"command": "ls"},
            )
            msg = Message(role="assistant", content="", tool_calls=[tc])
            return ProviderResponse(
                message=msg,
                stop_reason="tool_use",
                usage=Usage(input_tokens=1, output_tokens=1),
            )

        async def stream_complete(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError

    # Tool stub registry: dispatch returns a constant ToolResult so the
    # args_hash for Bash({"command":"ls"}) is identical every iteration.
    from opencomputer.tools.registry import registry as _registry
    from plugin_sdk.core import ToolResult
    from plugin_sdk.tool_contract import BaseTool, ToolSchema

    class StubBashTool(BaseTool):
        @property
        def schema(self) -> ToolSchema:
            return ToolSchema(
                name="Bash",
                description="stub",
                input_schema={"type": "object", "properties": {}},
            )

        async def execute(self, arguments, *, session_id="", turn_index=0):
            return ToolResult(tool_call_id="ignored", content="ok", is_error=False)

    _registry.register(StubBashTool(), replace=True)

    # Build a minimal Config pointing the SQLite DB into tmp_path.
    cfg = Config(
        model=ModelConfig(model="stub", max_tokens=1024, temperature=0.0),
        loop=LoopConfig(max_iterations=20),
        session=SessionConfig(db_path=tmp_path / "sessions.db"),
        memory=MemoryConfig(
            declarative_path=tmp_path / "MEMORY.md",
            skills_path=tmp_path / "skills",
            user_path=tmp_path / "USER.md",
            soul_path=tmp_path / "SOUL.md",
        ),
    )

    loop = AgentLoop(provider=StubProvider(), config=cfg)
    # Guard: shorten thresholds so the test is fast and deterministic.
    loop._loop_detector.max_tool_repeats = 2
    loop._loop_detector.max_consecutive_flags = 2

    result = await loop.run_conversation("please list files")

    # The final message must be a synthetic abort notice, not a tool-loop
    # exhaustion message.
    assert "Agent loop stopped" in (result.final_message.content or "")
