"""Tests for per-hook timeout_ms field."""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.hooks.engine import HookEngine
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec


@pytest.fixture
def engine():
    return HookEngine()


@pytest.fixture
def ctx():
    return HookContext(event=HookEvent.PRE_TOOL_USE, session_id="test")


@pytest.mark.asyncio
async def test_hook_with_no_timeout_runs_to_completion(engine, ctx):
    called: list[bool] = []

    async def slow(c: HookContext) -> HookDecision:
        await asyncio.sleep(0.05)
        called.append(True)
        return HookDecision(decision="pass")

    engine.register(HookSpec(event=HookEvent.PRE_TOOL_USE, handler=slow))
    decision = await engine.fire_blocking(ctx)
    assert called == [True]
    assert decision is None  # all-pass → None


@pytest.mark.asyncio
async def test_hook_timeout_fails_open(engine, ctx):
    """Hook that exceeds timeout_ms is treated as 'pass' with a warning."""
    called: list[bool] = []

    async def slow(c: HookContext) -> HookDecision:
        await asyncio.sleep(2.0)  # way over timeout
        called.append(True)
        return HookDecision(decision="block", reason="should not reach")

    engine.register(
        HookSpec(event=HookEvent.PRE_TOOL_USE, handler=slow, timeout_ms=50)
    )
    decision = await engine.fire_blocking(ctx)
    assert decision is None  # fail-open → no block
    assert called == []  # the slow hook was cancelled


@pytest.mark.asyncio
async def test_hook_timeout_zero_treated_as_no_timeout(engine, ctx):
    """timeout_ms=0 must NOT raise immediately — treat as None."""
    called: list[bool] = []

    async def fast(c: HookContext) -> HookDecision:
        called.append(True)
        return HookDecision(decision="pass")

    engine.register(
        HookSpec(event=HookEvent.PRE_TOOL_USE, handler=fast, timeout_ms=0)
    )
    decision = await engine.fire_blocking(ctx)
    assert called == [True]
    assert decision is None


@pytest.mark.asyncio
async def test_hook_timeout_does_not_affect_other_hooks(engine, ctx):
    """Slow hook's timeout doesn't stop subsequent hooks from running."""
    order: list[str] = []

    async def slow(c: HookContext) -> HookDecision:
        await asyncio.sleep(2.0)
        order.append("slow")
        return HookDecision(decision="pass")

    async def fast(c: HookContext) -> HookDecision:
        order.append("fast")
        return HookDecision(decision="pass")

    engine.register(
        HookSpec(event=HookEvent.PRE_TOOL_USE, handler=slow, timeout_ms=50, priority=50)
    )
    engine.register(
        HookSpec(event=HookEvent.PRE_TOOL_USE, handler=fast, priority=200)
    )
    await engine.fire_blocking(ctx)
    # slow timed out; fast still ran (subsequent priority bucket)
    assert "fast" in order
    assert "slow" not in order
