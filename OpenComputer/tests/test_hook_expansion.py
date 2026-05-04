"""Round 2A P-1 — hook expansion: 8 new events + priority field.

Covers:
- All 17 events round-trip through the engine (handler is invoked).
- Priority ordering: lower runs first; FIFO within the same priority bucket.
- Backwards compat: HookSpec without ``priority`` defaults to 100.
- HookContext optional fields default to None (back-compat).
- TRANSFORM_TOOL_RESULT modified_message replaces the result content.
- PRE_LLM_CALL fires with messages list snapshot via the agent loop.
"""

from __future__ import annotations

from typing import Any

import pytest

from opencomputer.hooks.engine import HookEngine
from plugin_sdk import (
    ALL_HOOK_EVENTS,
    HookContext,
    HookDecision,
    HookEvent,
    HookSpec,
)

# ─── ALL_HOOK_EVENTS round-trip ────────────────────────────────────────


def test_all_events_present_in_all_hook_events_tuple() -> None:
    """Every HookEvent value must appear in ALL_HOOK_EVENTS exactly once.

    Wave 5 T13/T14 added three more events (PRE_GATEWAY_DISPATCH,
    PRE_APPROVAL_REQUEST, POST_APPROVAL_RESPONSE) for a total of 20.
    """
    assert len(ALL_HOOK_EVENTS) == 20
    assert set(ALL_HOOK_EVENTS) == set(HookEvent)
    # No duplicates.
    assert len(set(ALL_HOOK_EVENTS)) == len(ALL_HOOK_EVENTS)


def test_eight_new_events_have_correct_string_values() -> None:
    """The 8 new events must serialize to the documented PascalCase strings."""
    assert HookEvent.PRE_LLM_CALL.value == "PreLLMCall"
    assert HookEvent.POST_LLM_CALL.value == "PostLLMCall"
    assert HookEvent.TRANSFORM_TOOL_RESULT.value == "TransformToolResult"
    assert HookEvent.TRANSFORM_TERMINAL_OUTPUT.value == "TransformTerminalOutput"
    assert HookEvent.BEFORE_PROMPT_BUILD.value == "BeforePromptBuild"
    assert HookEvent.BEFORE_COMPACTION.value == "BeforeCompaction"
    assert HookEvent.AFTER_COMPACTION.value == "AfterCompaction"
    assert HookEvent.BEFORE_MESSAGE_WRITE.value == "BeforeMessageWrite"


def test_all_hook_events_declaration_order_preserves_originals_first() -> None:
    """First 9 entries must be the original events in their original order."""
    original = (
        HookEvent.PRE_TOOL_USE,
        HookEvent.POST_TOOL_USE,
        HookEvent.STOP,
        HookEvent.SESSION_START,
        HookEvent.SESSION_END,
        HookEvent.USER_PROMPT_SUBMIT,
        HookEvent.PRE_COMPACT,
        HookEvent.SUBAGENT_STOP,
        HookEvent.NOTIFICATION,
    )
    assert ALL_HOOK_EVENTS[: len(original)] == original


async def test_every_event_round_trips_through_engine() -> None:
    """Every HookEvent value reaches its registered handler when fired."""
    eng = HookEngine()
    seen: list[HookEvent] = []

    async def _handler_for(event: HookEvent):
        async def _h(ctx: HookContext) -> HookDecision | None:
            seen.append(ctx.event)
            return None

        return _h

    for event in ALL_HOOK_EVENTS:
        eng.register(HookSpec(event=event, handler=await _handler_for(event)))

    for event in ALL_HOOK_EVENTS:
        await eng.fire_blocking(HookContext(event=event, session_id="s"))

    assert seen == list(ALL_HOOK_EVENTS)


# ─── HookContext extensions ────────────────────────────────────────────


def test_hookcontext_new_optional_fields_default_to_none() -> None:
    """All 4 new HookContext fields default to None for back-compat."""
    ctx = HookContext(event=HookEvent.STOP, session_id="s")
    assert ctx.prompt_text is None
    assert ctx.messages is None
    assert ctx.streamed_chunk is None
    assert ctx.model is None


def test_hookcontext_accepts_new_fields_when_provided() -> None:
    """Each new field round-trips through construction."""
    msgs: list[Any] = ["m1", "m2"]
    ctx = HookContext(
        event=HookEvent.PRE_LLM_CALL,
        session_id="s",
        prompt_text="hello",
        messages=msgs,
        streamed_chunk="chunk",
        model="claude-x",
    )
    assert ctx.prompt_text == "hello"
    assert ctx.messages == ["m1", "m2"]
    assert ctx.streamed_chunk == "chunk"
    assert ctx.model == "claude-x"


# ─── HookSpec.priority ─────────────────────────────────────────────────


def test_hookspec_priority_defaults_to_100() -> None:
    """Specs without an explicit priority default to 100 (the FIFO bucket)."""

    async def _h(ctx: HookContext) -> HookDecision | None:
        return None

    spec = HookSpec(event=HookEvent.STOP, handler=_h)
    assert spec.priority == 100


async def test_priority_lower_runs_first() -> None:
    """priority=10 must run before priority=100 even if registered later."""
    eng = HookEngine()
    order: list[str] = []

    async def _high(ctx: HookContext) -> HookDecision | None:
        order.append("high")
        return None

    async def _low(ctx: HookContext) -> HookDecision | None:
        order.append("low")
        return None

    # Register the higher-priority (slower) one first; the lower-priority
    # handler must still run first because it has the smaller priority value.
    eng.register(HookSpec(event=HookEvent.STOP, handler=_high, priority=100))
    eng.register(HookSpec(event=HookEvent.STOP, handler=_low, priority=10))

    await eng.fire_blocking(HookContext(event=HookEvent.STOP, session_id="s"))
    assert order == ["low", "high"]


async def test_same_priority_preserves_fifo_registration_order() -> None:
    """Within a priority bucket, FIFO (registration order) is preserved."""
    eng = HookEngine()
    order: list[str] = []

    async def _make(name: str):
        async def _h(ctx: HookContext) -> HookDecision | None:
            order.append(name)
            return None

        return _h

    eng.register(HookSpec(event=HookEvent.STOP, handler=await _make("a"), priority=50))
    eng.register(HookSpec(event=HookEvent.STOP, handler=await _make("b"), priority=50))
    eng.register(HookSpec(event=HookEvent.STOP, handler=await _make("c"), priority=50))

    await eng.fire_blocking(HookContext(event=HookEvent.STOP, session_id="s"))
    assert order == ["a", "b", "c"]


async def test_backwards_compat_default_priority_runs_in_fifo() -> None:
    """Specs without any ``priority=`` kwarg behave like the pre-2A engine."""
    eng = HookEngine()
    order: list[str] = []

    async def _make(name: str):
        async def _h(ctx: HookContext) -> HookDecision | None:
            order.append(name)
            return None

        return _h

    # No priority argument — relies on the default value of 100.
    eng.register(HookSpec(event=HookEvent.STOP, handler=await _make("first")))
    eng.register(HookSpec(event=HookEvent.STOP, handler=await _make("second")))
    eng.register(HookSpec(event=HookEvent.STOP, handler=await _make("third")))

    await eng.fire_blocking(HookContext(event=HookEvent.STOP, session_id="s"))
    assert order == ["first", "second", "third"]


async def test_mixed_priorities_with_ties_keep_fifo_within_buckets() -> None:
    """Priorities sort across buckets; within each bucket FIFO is preserved."""
    eng = HookEngine()
    order: list[str] = []

    async def _make(name: str):
        async def _h(ctx: HookContext) -> HookDecision | None:
            order.append(name)
            return None

        return _h

    eng.register(HookSpec(event=HookEvent.STOP, handler=await _make("p100-1"), priority=100))
    eng.register(HookSpec(event=HookEvent.STOP, handler=await _make("p10-1"), priority=10))
    eng.register(HookSpec(event=HookEvent.STOP, handler=await _make("p100-2"), priority=100))
    eng.register(HookSpec(event=HookEvent.STOP, handler=await _make("p10-2"), priority=10))
    eng.register(HookSpec(event=HookEvent.STOP, handler=await _make("p50-1"), priority=50))

    await eng.fire_blocking(HookContext(event=HookEvent.STOP, session_id="s"))
    assert order == ["p10-1", "p10-2", "p50-1", "p100-1", "p100-2"]


# ─── TRANSFORM_TOOL_RESULT semantics ───────────────────────────────────


async def test_transform_tool_result_modified_message_replaces_result() -> None:
    """A handler returning ``modified_message`` must rewrite the tool result text."""
    from opencomputer.agent.loop import _maybe_transform_tool_result
    from opencomputer.hooks.engine import engine as global_engine
    from plugin_sdk.core import ToolCall, ToolResult
    from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT

    # Reset the global engine for this event so other tests don't pollute.
    global_engine.unregister_all(HookEvent.TRANSFORM_TOOL_RESULT)

    async def _rewriter(ctx: HookContext) -> HookDecision:
        # Verify the hook sees the original tool_result before rewriting.
        assert ctx.tool_result is not None
        assert ctx.tool_result.content == "original output"
        return HookDecision(
            decision="approve",
            modified_message="rewritten output",
        )

    global_engine.register(
        HookSpec(event=HookEvent.TRANSFORM_TOOL_RESULT, handler=_rewriter)
    )

    call = ToolCall(id="tc-1", name="Bash", arguments={"command": "echo hi"})
    original = ToolResult(tool_call_id="tc-1", content="original output")

    rewritten = await _maybe_transform_tool_result(
        result=original,
        call=call,
        session_id="sid",
        runtime=DEFAULT_RUNTIME_CONTEXT,
    )
    assert rewritten.content == "rewritten output"
    assert rewritten.tool_call_id == "tc-1"
    assert rewritten.is_error is False

    # Cleanup so subsequent tests start fresh.
    global_engine.unregister_all(HookEvent.TRANSFORM_TOOL_RESULT)


async def test_transform_tool_result_no_handler_passthrough() -> None:
    """When no handler is registered, the result must come back untouched."""
    from opencomputer.agent.loop import _maybe_transform_tool_result
    from opencomputer.hooks.engine import engine as global_engine
    from plugin_sdk.core import ToolCall, ToolResult
    from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT

    global_engine.unregister_all(HookEvent.TRANSFORM_TOOL_RESULT)

    call = ToolCall(id="tc-2", name="Read", arguments={})
    original = ToolResult(tool_call_id="tc-2", content="unchanged")

    out = await _maybe_transform_tool_result(
        result=original,
        call=call,
        session_id="sid",
        runtime=DEFAULT_RUNTIME_CONTEXT,
    )
    assert out is original


async def test_transform_terminal_output_replaces_chunk() -> None:
    """TRANSFORM_TERMINAL_OUTPUT mirrors TRANSFORM_TOOL_RESULT for Bash chunks."""
    from opencomputer.agent.loop import _maybe_transform_terminal_output
    from opencomputer.hooks.engine import engine as global_engine
    from plugin_sdk.core import ToolCall, ToolResult
    from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT

    global_engine.unregister_all(HookEvent.TRANSFORM_TERMINAL_OUTPUT)

    async def _redact(ctx: HookContext) -> HookDecision:
        # streamed_chunk should mirror the result.content for the wrapper.
        assert ctx.streamed_chunk == "secret=abc123"
        return HookDecision(
            decision="approve",
            modified_message="secret=<REDACTED>",
        )

    global_engine.register(
        HookSpec(event=HookEvent.TRANSFORM_TERMINAL_OUTPUT, handler=_redact)
    )

    call = ToolCall(id="tc-3", name="Bash", arguments={"command": "echo $SECRET"})
    original = ToolResult(tool_call_id="tc-3", content="secret=abc123")
    out = await _maybe_transform_terminal_output(
        result=original,
        call=call,
        session_id="sid",
        runtime=DEFAULT_RUNTIME_CONTEXT,
    )
    assert out.content == "secret=<REDACTED>"

    global_engine.unregister_all(HookEvent.TRANSFORM_TERMINAL_OUTPUT)


# ─── PRE_LLM_CALL via agent loop ───────────────────────────────────────


async def test_pre_llm_call_fires_with_messages_list(tmp_path) -> None:
    """When a turn runs, PRE_LLM_CALL must fire with the messages snapshot."""
    import asyncio

    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.state import SessionDB
    from opencomputer.hooks.engine import engine as global_engine
    from plugin_sdk.core import (
        Message as _Message,
    )
    from plugin_sdk.provider_contract import (
        BaseProvider,
        ProviderResponse,
        Usage,
    )

    captured: dict[str, Any] = {}

    async def _on_pre(ctx: HookContext) -> HookDecision | None:
        captured["fired"] = True
        captured["model"] = ctx.model
        captured["messages"] = ctx.messages
        return None

    global_engine.unregister_all(HookEvent.PRE_LLM_CALL)
    global_engine.unregister_all(HookEvent.POST_LLM_CALL)
    global_engine.register(
        HookSpec(event=HookEvent.PRE_LLM_CALL, handler=_on_pre, fire_and_forget=False)
    )

    class _FakeProvider(BaseProvider):
        async def complete(
            self, *, model, messages, system=None, tools=None,
            max_tokens=None, temperature=None, **kw
        ):
            return ProviderResponse(
                message=_Message(role="assistant", content="ok"),
                stop_reason="end_turn",
                usage=Usage(input_tokens=5, output_tokens=2),
            )

        async def stream_complete(self, **kw):  # pragma: no cover — unused
            yield  # type: ignore[misc]

    # Minimal config — point session DB at the pytest tmp dir.
    from opencomputer.agent.config import Config, SessionConfig

    db_path = tmp_path / "sessions.db"
    cfg = Config(session=SessionConfig(db_path=db_path))
    db = SessionDB(db_path)
    loop = AgentLoop(
        provider=_FakeProvider(),
        config=cfg,
        db=db,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )

    result = await loop.run_conversation("hi", session_id="sid-pre-llm")
    # Wait briefly for fire-and-forget hook tasks to settle.
    await asyncio.sleep(0.05)

    assert captured.get("fired") is True
    assert captured.get("model") == cfg.model.model
    assert captured.get("messages") is not None
    # The message snapshot must include the user message we just submitted.
    assert any(
        getattr(m, "role", None) == "user"
        and (getattr(m, "content", None) or "") == "hi"
        for m in (captured["messages"] or [])
    )
    assert result.final_message.content == "ok"

    global_engine.unregister_all(HookEvent.PRE_LLM_CALL)
    global_engine.unregister_all(HookEvent.POST_LLM_CALL)


# ─── HookSpec equality / hashability ───────────────────────────────────


def test_hookspec_remains_frozen_dataclass() -> None:
    """HookSpec keeps its frozen+slots contract — plugins rely on equality."""
    from dataclasses import FrozenInstanceError

    async def _h(ctx: HookContext) -> HookDecision | None:
        return None

    spec = HookSpec(event=HookEvent.STOP, handler=_h, priority=42)
    with pytest.raises(FrozenInstanceError):
        spec.priority = 99  # type: ignore[misc]
