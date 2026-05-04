"""Tests for duration_ms on POST_TOOL_USE / TRANSFORM_TOOL_RESULT (Wave 5 T15).

Hermes-port (59b56d445) — also matches Claude Code 2.1.119. The agent
loop captures the tool dispatch latency once per call and forwards it
to both POST_TOOL_USE (observer) and TRANSFORM_TOOL_RESULT (rewriter)
so plugins can build per-tool SLO dashboards without manually
wrapping every tool.
"""

from __future__ import annotations

import pytest

from opencomputer.hooks.engine import HookEngine
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec


@pytest.mark.asyncio
async def test_post_tool_use_receives_duration_ms():
    """duration_ms is a non-negative int passed in HookContext."""
    engine = HookEngine()
    captured = {}

    async def hook(ctx):
        captured["ms"] = ctx.duration_ms
        return HookDecision(decision="pass")

    engine.register(HookSpec(event=HookEvent.POST_TOOL_USE, handler=hook))
    # Synthesize a hook fire with a known duration_ms; the loop's
    # actual fire site computes this same way.
    await engine.fire_blocking(HookContext(
        event=HookEvent.POST_TOOL_USE,
        session_id="s",
        duration_ms=125,
    ))
    assert isinstance(captured["ms"], int)
    assert captured["ms"] == 125


@pytest.mark.asyncio
async def test_transform_tool_result_receives_duration_ms():
    engine = HookEngine()
    captured = {}

    async def hook(ctx):
        captured["ms"] = ctx.duration_ms
        return HookDecision(decision="pass")

    engine.register(
        HookSpec(event=HookEvent.TRANSFORM_TOOL_RESULT, handler=hook),
    )
    await engine.fire_blocking(HookContext(
        event=HookEvent.TRANSFORM_TOOL_RESULT,
        session_id="s",
        duration_ms=37,
    ))
    assert captured["ms"] == 37


def test_duration_ms_defaults_to_none_for_back_compat():
    """Hooks written before Wave 5 don't pass duration_ms — None is fine."""
    ctx = HookContext(event=HookEvent.POST_TOOL_USE, session_id="s")
    assert ctx.duration_ms is None
