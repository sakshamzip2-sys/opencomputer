"""Tests for the LoopDetector + LoopAbortError.

Per AMENDMENTS H5 fix, the detector is scoped per ``(session_id,
delegation_depth)``. Each delegated subagent owns its own frame so a
parent's repetition history can't poison a child's window (and vice versa).
"""
from __future__ import annotations

import pytest

# ─── single-frame happy paths ─────────────────────────────────────────


def test_detector_flags_third_repeat_tool_call():
    """The flag-vs-must_stop distinction: a flag fires before a must_stop.

    ``max_consecutive_flags=2`` is set explicitly so this test isolates
    the flag step (the M1 default is 1, which collapses flag+must_stop
    onto the same call — covered by the enforce-mode tests below).
    """
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector(
        max_tool_repeats=3,
        max_text_repeats=2,
        window_size=8,
        max_consecutive_flags=2,
    )
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

    In ``enforce`` mode the detector with shortened thresholds
    (max_tool_repeats=2, max_consecutive_flags=2) flips to ``must_stop``
    after the second-and-third identical tool call inside the same
    window. The loop must surface a ``LoopAbortError`` and convert it to
    a clean ``"Agent loop stopped: ..."`` final message rather than
    letting the model spin to ``max_iterations``. ``mode="enforce"`` is
    required — observe mode (the M1 default) logs but never halts.
    """
    from opencomputer.agent.config import (
        Config,
        LoopConfig,
        MemoryConfig,
        ModelConfig,
        RepetitionDetectorConfig,
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
            # M1 (2026-05-16): isolate audit.db — the loop-abort path now
            # writes a tool_loop_trips row to ``config.home/audit.db``.
            home=tmp_path,
            model=ModelConfig(provider="stub", model="stub-model"),
            loop=LoopConfig(
                max_iterations=20,
                # enforce mode: a trip must HALT, not just log.
                repetition=RepetitionDetectorConfig(mode="enforce"),
            ),
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


# ─── Milestone-1 parity: config + audit + loop_safe (2026-05-16) ──────
#
# The detector itself already shipped (OpenClaw 1.C). M1 added three
# things on top: config-tunable thresholds, an audit.db trip log, and a
# per-tool ``loop_safe`` opt-out. These tests cover those additions.

_STUB_TOOL_NAME = "_LoopParityStubTool"


def test_repetition_config_defaults_match_detector_defaults() -> None:
    """The config block's defaults must equal LoopDetector's own — so a
    config that omits ``loop.repetition`` keeps the prior behavior exactly."""
    from opencomputer.agent.config import RepetitionDetectorConfig
    from opencomputer.agent.loop_safety import LoopDetector

    rc = RepetitionDetectorConfig()
    d = LoopDetector()
    assert (
        rc.max_tool_repeats,
        rc.max_text_repeats,
        rc.window_size,
        rc.max_consecutive_flags,
    ) == (
        d.max_tool_repeats,
        d.max_text_repeats,
        d.window_size,
        d.max_consecutive_flags,
    )


def test_default_config_carries_repetition_block() -> None:
    from opencomputer.agent.config import RepetitionDetectorConfig, default_config

    assert isinstance(default_config().loop.repetition, RepetitionDetectorConfig)


def test_record_loop_trip_creates_table_and_row(tmp_path) -> None:
    import sqlite3

    from opencomputer.agent.loop_safety import record_loop_trip

    db = tmp_path / "audit.db"
    record_loop_trip(db, session_id="s1", depth=0, kind="tool", detail="Bash x3")
    record_loop_trip(db, session_id="s1", depth=1, kind="text", detail="repeat")
    rows = (
        sqlite3.connect(db)
        .execute(
            "SELECT session_id, depth, kind, detail FROM tool_loop_trips ORDER BY id"
        )
        .fetchall()
    )
    assert rows == [("s1", 0, "tool", "Bash x3"), ("s1", 1, "text", "repeat")]


def test_record_loop_trip_swallows_db_errors(tmp_path) -> None:
    """A bad audit-db path must not raise — loop telemetry is best-effort."""
    from opencomputer.agent.loop_safety import record_loop_trip

    # Parent dir absent → sqlite error, swallowed at WARNING; must not raise.
    record_loop_trip(
        tmp_path / "missing_dir" / "audit.db",
        session_id="s",
        depth=0,
        kind="tool",
        detail="x",
    )


def test_basetool_loop_safe_defaults_false() -> None:
    from plugin_sdk.tool_contract import BaseTool

    assert BaseTool.loop_safe is False


def _build_stub_loop(tmp_path, *, loop_safe, max_iterations=20, repetition=None):
    """Build an AgentLoop driven by a provider that repeats one tool call.

    The stub tool's ``loop_safe`` class attr is set from ``loop_safe``. The
    caller MUST unregister :data:`_STUB_TOOL_NAME` in a ``finally`` (the name
    is a module constant so teardown works even if this raises).
    """
    from opencomputer.agent.config import (
        Config,
        LoopConfig,
        MemoryConfig,
        ModelConfig,
        RepetitionDetectorConfig,
        SessionConfig,
    )
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.tools.registry import registry as _registry
    from plugin_sdk.core import Message, ToolCall, ToolResult
    from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage
    from plugin_sdk.tool_contract import BaseTool, ToolSchema

    class _StubProvider(BaseProvider):
        def __init__(self) -> None:
            self.complete_calls = 0
            self.name = "stub"

        async def complete(self, **_kwargs):
            self.complete_calls += 1
            return ProviderResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=f"call-{self.complete_calls}",
                            name=_STUB_TOOL_NAME,
                            arguments={"command": "ls"},
                        )
                    ],
                ),
                stop_reason="tool_use",
                usage=Usage(input_tokens=1, output_tokens=1),
            )

        async def stream_complete(self, **_kwargs):  # pragma: no cover
            raise NotImplementedError

    class _StubTool(BaseTool):
        @property
        def schema(self) -> ToolSchema:
            return ToolSchema(
                name=_STUB_TOOL_NAME,
                description="loop-parity test stub",
                parameters={"type": "object", "properties": {}},
            )

        async def execute(self, call: ToolCall) -> ToolResult:
            return ToolResult(tool_call_id=call.id, content="ok", is_error=False)

    # Class body can't read an enclosing function local — set it after.
    _StubTool.loop_safe = loop_safe

    _registry.unregister(_STUB_TOOL_NAME)  # idempotent
    _registry.register(_StubTool())

    cfg = Config(
        home=tmp_path,
        model=ModelConfig(provider="stub", model="stub-model"),
        loop=LoopConfig(
            max_iterations=max_iterations,
            repetition=repetition or RepetitionDetectorConfig(),
        ),
        session=SessionConfig(db_path=tmp_path / "sessions.db"),
        memory=MemoryConfig(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
            skills_path=tmp_path / "skills",
            soul_path=tmp_path / "SOUL.md",
        ),
    )
    return AgentLoop(provider=_StubProvider(), config=cfg)


def test_agent_loop_wires_repetition_thresholds_from_config(tmp_path) -> None:
    """``loop.repetition`` config flows into the detector at AgentLoop init."""
    from opencomputer.agent.config import RepetitionDetectorConfig
    from opencomputer.tools.registry import registry as _registry

    try:
        loop = _build_stub_loop(
            tmp_path,
            loop_safe=False,
            repetition=RepetitionDetectorConfig(
                max_tool_repeats=5,
                max_text_repeats=4,
                window_size=7,
                max_consecutive_flags=3,
            ),
        )
        det = loop._loop_detector
        assert det.max_tool_repeats == 5
        assert det.max_text_repeats == 4
        assert det.window_size == 7
        assert det.max_consecutive_flags == 3
    finally:
        _registry.unregister(_STUB_TOOL_NAME)


@pytest.mark.asyncio
async def test_loop_safe_tool_is_exempt_from_repetition_abort(tmp_path) -> None:
    """A ``loop_safe`` tool must NOT trip the detector even when repeated."""
    from opencomputer.tools.registry import registry as _registry

    try:
        loop = _build_stub_loop(tmp_path, loop_safe=True, max_iterations=6)
        # Tighten so a NON-safe tool would abort fast — proves the exemption.
        loop._loop_detector.max_tool_repeats = 2
        loop._loop_detector.max_consecutive_flags = 2
        result = await loop.run_conversation(
            user_message="poll please",
            session_id="loop-parity-safe-1",
        )
    finally:
        _registry.unregister(_STUB_TOOL_NAME)
    content = result.final_message.content or ""
    assert "Agent loop stopped" not in content  # NOT loop-aborted
    assert "budget exhausted" in content  # ran to the iteration budget


@pytest.mark.asyncio
async def test_loop_abort_writes_a_tool_loop_trips_audit_row(tmp_path) -> None:
    """An enforce-mode hard-stop records the trip to tool_loop_trips."""
    import sqlite3

    from opencomputer.agent.config import RepetitionDetectorConfig
    from opencomputer.tools.registry import registry as _registry

    try:
        loop = _build_stub_loop(
            tmp_path,
            loop_safe=False,
            max_iterations=20,
            repetition=RepetitionDetectorConfig(mode="enforce"),
        )
        loop._loop_detector.max_tool_repeats = 2
        loop._loop_detector.max_consecutive_flags = 2
        result = await loop.run_conversation(
            user_message="list files",
            session_id="loop-parity-audit-1",
        )
    finally:
        _registry.unregister(_STUB_TOOL_NAME)
    assert "Agent loop stopped" in (result.final_message.content or "")
    audit_db = tmp_path / "audit.db"
    assert audit_db.exists(), "audit.db was not created by the loop abort"
    rows = (
        sqlite3.connect(audit_db)
        .execute("SELECT session_id, kind FROM tool_loop_trips")
        .fetchall()
    )
    assert rows == [("loop-parity-audit-1", "tool")]


# ─── M1: observe vs enforce mode + shipped-default behaviour ──────────
#
# The earlier loop-detection tests mutate ``loop._loop_detector`` thresholds
# at runtime and so never exercise what the *shipped defaults* actually do.
# Per PART-2 §4.1, loop detection ships in OBSERVE mode by default: a trip
# is logged to ``audit.db`` but the agent is NOT halted. ENFORCE mode halts.
# These tests assert both, against the real default config.


def test_repetition_config_default_mode_is_observe() -> None:
    """Per PART-2 §4.1 the shipped default is observe-only (logs, no halt)."""
    from opencomputer.agent.config import RepetitionDetectorConfig

    assert RepetitionDetectorConfig().mode == "observe"


def test_repetition_config_default_window_and_threshold_match_m1_spec() -> None:
    """M1 'Done when': halt on a 3rd identical tool-call in an 8-call window.

    Shipped defaults must be: window 8, flag on the 3rd repeat
    (``max_tool_repeats=3``), ``must_stop`` on the first flag
    (``max_consecutive_flags=1``) — so the 3rd identical call halts.
    """
    from opencomputer.agent.config import RepetitionDetectorConfig
    from opencomputer.agent.loop_safety import LoopDetector

    rc = RepetitionDetectorConfig()
    assert rc.window_size == 8
    assert rc.max_tool_repeats == 3
    assert rc.max_consecutive_flags == 1

    # And the detector's own constructor defaults must agree (the config
    # block and the runtime class are two halves of the same contract).
    d = LoopDetector()
    assert (d.window_size, d.max_tool_repeats, d.max_consecutive_flags) == (8, 3, 1)


def test_default_detector_must_stops_on_third_identical_tool_call() -> None:
    """With the shipped defaults the 3rd identical (tool, args) must_stops.

    Pure-detector check — no agent loop. ``must_stop`` becoming True is
    what an ENFORCE-mode loop halts on; an OBSERVE-mode loop only logs it.
    """
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector()  # shipped defaults: window 8, repeats 3, flags 1
    d.push_frame("s", 0)

    d.record_tool_call("s", 0, "Bash", "h")
    assert not d.must_stop("s", 0)  # 1st
    d.record_tool_call("s", 0, "Bash", "h")
    assert not d.must_stop("s", 0)  # 2nd
    d.record_tool_call("s", 0, "Bash", "h")
    assert d.must_stop("s", 0)  # 3rd — trips


@pytest.mark.asyncio
async def test_default_config_observe_mode_logs_but_does_not_halt(tmp_path) -> None:
    """Default config (observe): 3+ identical calls log a trip, NO halt.

    The agent must run to the iteration budget (NOT loop-abort) AND a
    single ``tool_loop_trips`` row must be written for the episode.
    """
    import sqlite3

    from opencomputer.tools.registry import registry as _registry

    try:
        # No ``repetition=`` → the M1 default config: observe mode, the
        # 8-call window, flag-on-3rd. Thresholds are NOT mutated.
        loop = _build_stub_loop(tmp_path, loop_safe=False, max_iterations=8)
        result = await loop.run_conversation(
            user_message="list files",
            session_id="observe-mode-1",
        )
    finally:
        _registry.unregister(_STUB_TOOL_NAME)

    content = result.final_message.content or ""
    # Observe mode must NOT halt the agent.
    assert "Agent loop stopped" not in content
    assert "budget exhausted" in content
    assert result.iterations == 8

    # But the trip is still logged — that is the whole point of observe
    # mode (calibration data). Exactly ONE row per trip episode: the
    # provider repeats the same call every turn with no reset between,
    # so ``claim_trip`` latches after the first must_stop.
    audit_db = tmp_path / "audit.db"
    assert audit_db.exists(), "observe mode must still write the trip log"
    rows = (
        sqlite3.connect(audit_db)
        .execute("SELECT session_id, kind FROM tool_loop_trips")
        .fetchall()
    )
    assert rows == [("observe-mode-1", "tool")], (
        f"observe mode should log exactly one trip per episode, got {rows!r}"
    )


@pytest.mark.asyncio
async def test_enforce_mode_halts_on_third_identical_tool_call(tmp_path) -> None:
    """Enforce mode + the shipped 3-in-8 default: the 3rd call halts.

    Thresholds are NOT mutated — only ``mode`` is flipped to enforce. The
    loop must stop well before its 8-iteration budget (the 3rd identical
    call trips), and surface a clean 'Agent loop stopped' message.
    """
    from opencomputer.agent.config import RepetitionDetectorConfig
    from opencomputer.tools.registry import registry as _registry

    try:
        loop = _build_stub_loop(
            tmp_path,
            loop_safe=False,
            max_iterations=8,
            repetition=RepetitionDetectorConfig(mode="enforce"),
        )
        result = await loop.run_conversation(
            user_message="list files",
            session_id="enforce-mode-1",
        )
    finally:
        _registry.unregister(_STUB_TOOL_NAME)

    content = result.final_message.content or ""
    assert "Agent loop stopped" in content, (
        f"enforce mode must halt the loop, got: {content!r}"
    )
    # 3rd identical call halts → strictly fewer than the 8-iteration budget.
    assert result.iterations < 8
    assert result.stop_reason is not None
    assert result.stop_reason.value == "tool_loop"


@pytest.mark.asyncio
async def test_enforce_mode_uses_tool_loop_stop_reason(tmp_path) -> None:
    """A loop trip surfaces ``StopReason.TOOL_LOOP`` — not a generic ERROR."""
    from opencomputer.agent.config import RepetitionDetectorConfig
    from opencomputer.tools.registry import registry as _registry
    from plugin_sdk.core import StopReason

    try:
        loop = _build_stub_loop(
            tmp_path,
            loop_safe=False,
            max_iterations=8,
            repetition=RepetitionDetectorConfig(mode="enforce"),
        )
        result = await loop.run_conversation(
            user_message="go",
            session_id="enforce-stopreason-1",
        )
    finally:
        _registry.unregister(_STUB_TOOL_NAME)

    assert result.stop_reason is StopReason.TOOL_LOOP


# ─── M1: window edge cases ────────────────────────────────────────────


def test_unique_call_between_repeats_resets_the_streak() -> None:
    """A unique tool call between repeats clears the consecutive-flag run.

    The detector's must_stop ramp counts *consecutive* flags. A unique
    (non-flagging) call resets ``consecutive_flags`` to 0, so the ramp
    has to climb again from zero before it can must_stop. This test
    pins that reset: a single flagged call after a unique one is only
    one flag — short of the 2-flag must_stop threshold.
    """
    from opencomputer.agent.loop_safety import LoopDetector

    # max_tool_repeats=2 so a pair flags; max_consecutive_flags=2 so a
    # single flag is not enough — two consecutive flags are needed.
    d = LoopDetector(max_tool_repeats=2, max_consecutive_flags=2, window_size=8)
    d.push_frame("s", 0)

    # First pair → 1 flag (count reaches 2). Not yet must_stop.
    d.record_tool_call("s", 0, "Bash", "h-A")
    d.record_tool_call("s", 0, "Bash", "h-A")
    assert d.flagged("s", 0)
    assert not d.must_stop("s", 0)

    # A second identical call WITHOUT a reset would be the 2nd consecutive
    # flag and would must_stop — confirm the ramp is genuinely armed here.
    probe = LoopDetector(max_tool_repeats=2, max_consecutive_flags=2, window_size=8)
    probe.push_frame("p", 0)
    for _ in range(3):
        probe.record_tool_call("p", 0, "Bash", "h-A")
    assert probe.must_stop("p", 0), "sanity: 3 in a row DOES must_stop"

    # Back to the real frame: a unique call resets consecutive_flags → 0.
    d.record_tool_call("s", 0, "Read", "h-B")
    assert not d.flagged("s", 0)
    assert not d.must_stop("s", 0)

    # One more identical call now flags AGAIN (the window still holds the
    # earlier "h-A" entries, so the count is ≥ 2) — but it is only the
    # FIRST flag of a fresh streak. Because the unique call reset the
    # counter, one flag is not enough: must_stop stays False.
    d.record_tool_call("s", 0, "Bash", "h-A")
    assert d.flagged("s", 0)
    assert not d.must_stop("s", 0), "the unique call reset the consecutive-flag ramp"


def test_window_maxlen_evicts_old_entries_slow_drip_never_trips() -> None:
    """A repeat slower than the window can hold never reaches the threshold.

    With window_size=8 and max_tool_repeats=3, a call that recurs once
    every 4 turns (interleaved with 3 unique calls) never has 3 copies
    co-resident in the 8-slot deque — so the detector must not flag.
    """
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector(max_tool_repeats=3, max_consecutive_flags=1, window_size=8)
    d.push_frame("s", 0)

    # 24 turns: every 4th turn is the repeated call, the other 3 unique.
    # At any moment the window holds at most 2 copies of "Poll" — so the
    # count never reaches 3 and the frame never flags / must_stops.
    for turn in range(24):
        if turn % 4 == 0:
            d.record_tool_call("s", 0, "Poll", "same-args")
        else:
            d.record_tool_call("s", 0, "Other", f"unique-{turn}")
        assert not d.must_stop("s", 0), f"slow drip tripped at turn {turn}"

    # Sanity: the deque really is bounded at window_size.
    assert len(d._frames[("s", 0)].tool_window) == 8


def test_three_identical_calls_within_window_do_trip() -> None:
    """Counterpart to the slow-drip test: 3 copies INSIDE the window trip.

    Proves the slow-drip test passes because of eviction, not because the
    detector is simply inert.
    """
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector(max_tool_repeats=3, max_consecutive_flags=1, window_size=8)
    d.push_frame("s", 0)

    # Two unique calls, then three identical ones — all five fit in the
    # 8-slot window, so the 3rd identical call must trip.
    d.record_tool_call("s", 0, "A", "u1")
    d.record_tool_call("s", 0, "B", "u2")
    d.record_tool_call("s", 0, "Poll", "args")
    d.record_tool_call("s", 0, "Poll", "args")
    assert not d.must_stop("s", 0)
    d.record_tool_call("s", 0, "Poll", "args")
    assert d.must_stop("s", 0)


def test_claim_trip_returns_true_once_per_episode() -> None:
    """``claim_trip`` latches: True the first must_stop, False after.

    This is what keeps observe mode from writing one audit row per
    iteration while the agent keeps repeating. The latch clears when a
    non-flagging call resets the streak, so a NEW episode can log again.
    """
    from opencomputer.agent.loop_safety import LoopDetector

    d = LoopDetector(max_tool_repeats=2, max_consecutive_flags=1, window_size=8)
    d.push_frame("s", 0)

    d.record_tool_call("s", 0, "Bash", "h")
    d.record_tool_call("s", 0, "Bash", "h")  # 2nd → flags → must_stop
    assert d.must_stop("s", 0)
    assert d.claim_trip("s", 0) is True  # first claim wins
    assert d.claim_trip("s", 0) is False  # latched — no duplicate log

    # Keep repeating: still latched (no duplicate audit rows mid-episode).
    d.record_tool_call("s", 0, "Bash", "h")
    assert d.must_stop("s", 0)
    assert d.claim_trip("s", 0) is False

    # A unique call resets the streak — and clears the latch.
    d.record_tool_call("s", 0, "Read", "other")
    assert not d.must_stop("s", 0)
    # A fresh repeat episode → claim_trip is armed again.
    d.record_tool_call("s", 0, "Bash", "h")
    d.record_tool_call("s", 0, "Bash", "h")
    assert d.must_stop("s", 0)
    assert d.claim_trip("s", 0) is True


@pytest.mark.asyncio
async def test_loop_safe_tool_calls_do_not_count_toward_detection(tmp_path) -> None:
    """A ``loop_safe=True`` tool is exempt — its calls never reach the window.

    Even in ENFORCE mode with the tightest thresholds, a loop_safe tool
    repeated every turn must not halt the agent: its calls are skipped
    before they are ever recorded, so the detector frame stays empty.
    """
    from opencomputer.agent.config import RepetitionDetectorConfig
    from opencomputer.tools.registry import registry as _registry

    try:
        loop = _build_stub_loop(
            tmp_path,
            loop_safe=True,
            max_iterations=6,
            repetition=RepetitionDetectorConfig(mode="enforce"),
        )
        # Tightest possible thresholds — a NON-safe tool would halt on the
        # 2nd call. Proves the exemption, not a lax threshold.
        loop._loop_detector.max_tool_repeats = 2
        loop._loop_detector.max_consecutive_flags = 1
        result = await loop.run_conversation(
            user_message="poll please",
            session_id="loop-safe-exempt-1",
        )
    finally:
        _registry.unregister(_STUB_TOOL_NAME)

    content = result.final_message.content or ""
    assert "Agent loop stopped" not in content, "loop_safe tool must be exempt"
    assert "budget exhausted" in content
    assert result.iterations == 6

    # And no trip was ever logged — exempt calls are never recorded.
    audit_db = tmp_path / "audit.db"
    if audit_db.exists():
        import sqlite3

        rows = (
            sqlite3.connect(audit_db)
            .execute("SELECT COUNT(*) FROM tool_loop_trips")
            .fetchone()
        )
        assert rows[0] == 0, "a loop_safe tool must produce no trip rows"
