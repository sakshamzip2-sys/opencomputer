"""Tests for Phase 3 hook events: BEFORE_MODEL_RESOLVE / MESSAGE_SENDING / MESSAGE_SENT."""

from __future__ import annotations


def test_phase3_hook_events_exist():
    from plugin_sdk.hooks import ALL_HOOK_EVENTS, HookEvent

    assert HookEvent.BEFORE_MODEL_RESOLVE.value == "BeforeModelResolve"
    assert HookEvent.MESSAGE_SENDING.value == "MessageSending"
    assert HookEvent.MESSAGE_SENT.value == "MessageSent"

    assert HookEvent.BEFORE_MODEL_RESOLVE in ALL_HOOK_EVENTS
    assert HookEvent.MESSAGE_SENDING in ALL_HOOK_EVENTS
    assert HookEvent.MESSAGE_SENT in ALL_HOOK_EVENTS


def test_phase3_hook_context_fields():
    from plugin_sdk.hooks import HookContext, HookEvent

    ctx = HookContext(
        event=HookEvent.BEFORE_MODEL_RESOLVE,
        session_id="s",
        pre_resolve_model="fast",
        model="fast",
    )
    assert ctx.pre_resolve_model == "fast"

    ctx2 = HookContext(
        event=HookEvent.MESSAGE_SENDING,
        session_id="s",
        outgoing_text="hello",
        channel="telegram",
        outgoing_chat_id="42",
    )
    assert ctx2.outgoing_text == "hello"
    assert ctx2.channel == "telegram"
    assert ctx2.outgoing_chat_id == "42"


async def test_before_model_resolve_hook_fires_on_resolve_path():
    """The hook engine receives BEFORE_MODEL_RESOLVE before resolve_model()."""
    from opencomputer.hooks.engine import engine as _engine
    from plugin_sdk.hooks import (
        HookContext,
        HookDecision,
        HookEvent,
        HookSpec,
    )

    fired: list[HookContext] = []

    async def handler(ctx: HookContext) -> HookDecision | None:
        fired.append(ctx)
        return None

    spec = HookSpec(event=HookEvent.BEFORE_MODEL_RESOLVE, handler=handler)
    _engine.register(spec)
    try:
        # Drive the engine directly (the loop wires this; here we
        # exercise the engine path so this test doesn't need the full
        # AgentLoop set up).
        decision = await _engine.fire_blocking(
            HookContext(
                event=HookEvent.BEFORE_MODEL_RESOLVE,
                session_id="t",
                pre_resolve_model="fast",
                model="fast",
            )
        )
        assert decision is None  # handler returned None → "pass"
    finally:
        _engine.unregister_all(HookEvent.BEFORE_MODEL_RESOLVE)

    assert len(fired) == 1
    assert fired[0].pre_resolve_model == "fast"


async def test_before_model_resolve_can_rewrite_alias():
    """A handler returning decision=rewrite is propagated to the caller."""
    from opencomputer.hooks.engine import engine as _engine
    from plugin_sdk.hooks import (
        HookContext,
        HookDecision,
        HookEvent,
        HookSpec,
    )

    async def handler(ctx: HookContext) -> HookDecision:
        return HookDecision(
            decision="rewrite",
            modified_message="claude-opus-4-7",
            reason="route long-context to opus",
        )

    spec = HookSpec(event=HookEvent.BEFORE_MODEL_RESOLVE, handler=handler)
    _engine.register(spec)
    try:
        decision = await _engine.fire_blocking(
            HookContext(
                event=HookEvent.BEFORE_MODEL_RESOLVE,
                session_id="t",
                pre_resolve_model="fast",
                model="fast",
            )
        )
    finally:
        _engine.unregister_all(HookEvent.BEFORE_MODEL_RESOLVE)

    assert decision is not None
    assert decision.decision == "rewrite"
    assert decision.modified_message == "claude-opus-4-7"


async def test_message_sending_hook_can_skip():
    """A MESSAGE_SENDING handler returning decision=skip is honoured."""
    from opencomputer.hooks.engine import engine as _engine
    from plugin_sdk.hooks import (
        HookContext,
        HookDecision,
        HookEvent,
        HookSpec,
    )

    async def handler(ctx: HookContext) -> HookDecision:
        return HookDecision(decision="skip", reason="local policy")

    spec = HookSpec(event=HookEvent.MESSAGE_SENDING, handler=handler)
    _engine.register(spec)
    try:
        decision = await _engine.fire_blocking(
            HookContext(
                event=HookEvent.MESSAGE_SENDING,
                session_id="t",
                outgoing_text="silent",
                channel="telegram",
                outgoing_chat_id="42",
            )
        )
    finally:
        _engine.unregister_all(HookEvent.MESSAGE_SENDING)

    assert decision is not None
    assert decision.decision == "skip"


async def test_message_sent_fire_and_forget_observability():
    """MESSAGE_SENT is fire-and-forget — no decision is returned."""
    from opencomputer.hooks.engine import engine as _engine
    from plugin_sdk.hooks import HookContext, HookEvent, HookSpec

    fired: list[HookContext] = []

    async def handler(ctx: HookContext) -> None:
        fired.append(ctx)

    spec = HookSpec(event=HookEvent.MESSAGE_SENT, handler=handler)
    _engine.register(spec)
    try:
        _engine.fire_and_forget(
            HookContext(
                event=HookEvent.MESSAGE_SENT,
                session_id="t",
                outgoing_text="delivered",
                channel="discord",
                outgoing_chat_id="9",
            )
        )
        # fire_and_forget schedules — give event loop a tick.
        import asyncio

        await asyncio.sleep(0.05)
    finally:
        _engine.unregister_all(HookEvent.MESSAGE_SENT)

    assert len(fired) >= 1
    assert fired[0].outgoing_text == "delivered"
