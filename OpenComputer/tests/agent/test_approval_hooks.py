"""Tests for PRE/POST_APPROVAL_REQUEST hooks (Wave 5 T14).

Hermes-port (30307a980). Both hooks are observer-only — return values
are ignored, plugin crashes are swallowed by the engine. The
``surface`` kwarg distinguishes "cli" from "gateway"; the ``choice``
kwarg on POST is one of: once|session|always|deny|timeout.
"""

from __future__ import annotations

import pytest

from opencomputer.hooks.engine import HookEngine
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec


@pytest.mark.asyncio
async def test_pre_approval_observed():
    engine = HookEngine()
    seen = []

    async def hook(ctx):
        seen.append((ctx.surface, ctx.command))
        return HookDecision(decision="pass")

    engine.register(HookSpec(event=HookEvent.PRE_APPROVAL_REQUEST, handler=hook))
    await engine.fire_blocking(HookContext(
        event=HookEvent.PRE_APPROVAL_REQUEST,
        session_id="s",
        surface="cli",
        command="rm -rf /",
    ))
    assert seen == [("cli", "rm -rf /")]


@pytest.mark.asyncio
async def test_post_approval_records_choice():
    engine = HookEngine()
    seen = []

    async def hook(ctx):
        seen.append((ctx.choice, ctx.surface, ctx.command))
        return HookDecision(decision="pass")

    engine.register(HookSpec(event=HookEvent.POST_APPROVAL_RESPONSE, handler=hook))
    await engine.fire_blocking(HookContext(
        event=HookEvent.POST_APPROVAL_RESPONSE,
        session_id="s",
        surface="gateway",
        command="rm -rf /",
        choice="deny",
    ))
    assert seen == [("deny", "gateway", "rm -rf /")]


@pytest.mark.asyncio
async def test_pre_approval_plugin_crash_swallowed():
    """Observer hook crashing must not affect the approval flow."""
    engine = HookEngine()

    async def boom(ctx):
        raise RuntimeError("plugin bug")

    engine.register(HookSpec(event=HookEvent.PRE_APPROVAL_REQUEST, handler=boom))
    d = await engine.fire_blocking(HookContext(
        event=HookEvent.PRE_APPROVAL_REQUEST,
        session_id="s",
        surface="cli",
        command="x",
    ))
    assert d is None  # all hooks crashed → engine returned None


@pytest.mark.asyncio
async def test_post_approval_all_choice_values():
    """All five canonical choice values flow through unchanged."""
    engine = HookEngine()
    seen: list[str] = []

    async def hook(ctx):
        seen.append(ctx.choice)
        return HookDecision(decision="pass")

    engine.register(HookSpec(event=HookEvent.POST_APPROVAL_RESPONSE, handler=hook))
    for choice in ("once", "session", "always", "deny", "timeout"):
        await engine.fire_blocking(HookContext(
            event=HookEvent.POST_APPROVAL_RESPONSE,
            session_id="s",
            surface="cli",
            command="x",
            choice=choice,
        ))
    assert seen == ["once", "session", "always", "deny", "timeout"]
