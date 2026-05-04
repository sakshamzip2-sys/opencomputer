"""Tests for PRE_GATEWAY_DISPATCH hook event (Wave 5 T13).

Hermes-port (1ef1e4c66). The hook fires once per inbound gateway
message. Plugins can:

- ``decision="skip"`` — drop the message silently
- ``decision="rewrite"`` + ``rewritten_text=...`` — replace the text
- ``decision="allow"`` / ``"pass"`` — proceed normally

Plugin crashes are swallowed by the hook engine (per the engine's
existing contract); the dispatch path then proceeds as if no plugin
was registered for this event.
"""

from __future__ import annotations

import pytest

from opencomputer.hooks.engine import HookEngine
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec


@pytest.mark.asyncio
async def test_pre_gateway_dispatch_skip_drops_message():
    engine = HookEngine()
    captured = []

    async def my_hook(ctx: HookContext) -> HookDecision:
        captured.append(ctx.gateway_event_text)
        return HookDecision(decision="skip", reason="filter")

    engine.register(HookSpec(event=HookEvent.PRE_GATEWAY_DISPATCH, handler=my_hook))
    decision = await engine.fire_blocking(HookContext(
        event=HookEvent.PRE_GATEWAY_DISPATCH,
        session_id="s1",
        gateway_event_text="hello",
        sender_id="user-1",
    ))
    assert decision is not None
    assert decision.decision == "skip"
    assert captured == ["hello"]


@pytest.mark.asyncio
async def test_pre_gateway_dispatch_rewrite():
    engine = HookEngine()

    async def hook(ctx):
        return HookDecision(
            decision="rewrite", rewritten_text="REWRITTEN",
        )

    engine.register(HookSpec(event=HookEvent.PRE_GATEWAY_DISPATCH, handler=hook))
    d = await engine.fire_blocking(HookContext(
        event=HookEvent.PRE_GATEWAY_DISPATCH, session_id="s1",
        gateway_event_text="orig", sender_id="u1",
    ))
    assert d is not None
    assert d.decision == "rewrite"
    assert d.rewritten_text == "REWRITTEN"


@pytest.mark.asyncio
async def test_plugin_crash_swallowed():
    """Hermes-port: a crashing plugin must not break the dispatch path.

    Per the engine contract (corrected from the plan draft): when every
    registered hook crashes, ``fire_blocking`` returns ``None`` (the
    "no opinion" verdict), not a synthetic pass decision.
    """
    engine = HookEngine()

    async def boom(ctx):
        raise RuntimeError("plugin bug")

    engine.register(HookSpec(event=HookEvent.PRE_GATEWAY_DISPATCH, handler=boom))
    d = await engine.fire_blocking(HookContext(
        event=HookEvent.PRE_GATEWAY_DISPATCH, session_id="s1",
        gateway_event_text="x", sender_id="u",
    ))
    assert d is None  # all hooks crashed → engine returned None


@pytest.mark.asyncio
async def test_pre_gateway_dispatch_allow_passes_through():
    """An explicit allow verdict still surfaces back to the caller."""
    engine = HookEngine()

    async def hook(ctx):
        return HookDecision(decision="allow")

    engine.register(HookSpec(event=HookEvent.PRE_GATEWAY_DISPATCH, handler=hook))
    d = await engine.fire_blocking(HookContext(
        event=HookEvent.PRE_GATEWAY_DISPATCH,
        session_id="s",
        gateway_event_text="hi",
    ))
    assert d is not None
    assert d.decision == "allow"


@pytest.mark.asyncio
async def test_pre_gateway_dispatch_pass_returns_none():
    """A "pass" decision is filtered by fire_blocking → caller sees None."""
    engine = HookEngine()

    async def hook(ctx):
        return HookDecision(decision="pass")

    engine.register(HookSpec(event=HookEvent.PRE_GATEWAY_DISPATCH, handler=hook))
    d = await engine.fire_blocking(HookContext(
        event=HookEvent.PRE_GATEWAY_DISPATCH,
        session_id="s",
        gateway_event_text="hi",
    ))
    assert d is None
