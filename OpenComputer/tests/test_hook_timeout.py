"""Tests for per-hook timeout_ms field."""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.agent import hook_history
from opencomputer.hooks.engine import HookEngine
from opencomputer.hooks.runner import _pending, drain_pending
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec


@pytest.fixture
def engine():
    return HookEngine()


@pytest.fixture
def ctx():
    return HookContext(event=HookEvent.PRE_TOOL_USE, session_id="test")


@pytest.fixture
def recorded(monkeypatch):
    """Capture every ``record_fire`` call as a list of dicts.

    ``HookEngine.fire_and_forget`` does ``from opencomputer.agent.hook_history
    import record_fire as _record`` at call time, so patching the attribute
    on the module is observed by the engine.
    """
    calls: list[dict] = []

    def _capture(event, source_id, *, ok, summary):
        calls.append({"event": event, "source_id": source_id, "ok": ok, "summary": summary})

    monkeypatch.setattr(hook_history, "record_fire", _capture)
    return calls


@pytest.fixture(autouse=True)
def _clear_pending():
    """Fire-and-forget tests must start + end with no in-flight tasks."""
    _pending.clear()
    yield
    _pending.clear()


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


# --- fire_and_forget honors timeout_ms ------------------------------------


@pytest.mark.asyncio
async def test_fire_and_forget_timeout_cancels_slow_handler(
    engine, ctx, recorded, caplog
):
    """A fire-and-forget hook with timeout_ms is cancelled at the timeout.

    Regression: ``_run_and_record`` awaited the handler with no
    ``asyncio.wait_for``, so timeout_ms was silently dropped and the slow
    handler ran to completion. After the fix, the handler is cancelled, a
    WARNING is logged, and ``ok=False`` with a timeout summary is recorded.
    """
    completed: list[bool] = []

    async def slow(c: HookContext) -> None:
        await asyncio.sleep(2.0)  # way over the 50ms timeout
        completed.append(True)  # only reached on full completion

    engine.register(
        HookSpec(event=HookEvent.POST_TOOL_USE, handler=slow, timeout_ms=50)
    )
    fire_ctx = HookContext(event=HookEvent.POST_TOOL_USE, session_id="test")

    with caplog.at_level("WARNING", logger="opencomputer.hooks"):
        engine.fire_and_forget(fire_ctx)
        done, cancelled = await drain_pending(timeout=2.0)

    # The slow handler was cancelled at the timeout, not run to completion.
    assert completed == []
    # The wait_for timed out cleanly (task finished, not drain-cancelled).
    assert (done, cancelled) == (1, 0)
    # A WARNING naming the timeout was logged.
    assert any("timed out" in r.message or "timeout" in r.message for r in caplog.records)
    # ok=False with a timeout summary was recorded.
    timeouts = [c for c in recorded if not c["ok"] and "timeout" in c["summary"]]
    assert len(timeouts) == 1
    assert timeouts[0]["event"] == HookEvent.POST_TOOL_USE.value
    assert "50ms" in timeouts[0]["summary"]


@pytest.mark.asyncio
async def test_fire_and_forget_fast_handler_with_timeout_completes(
    engine, ctx, recorded
):
    """A fast fire-and-forget handler under timeout_ms completes + records ok."""
    completed: list[bool] = []

    async def fast(c: HookContext) -> None:
        completed.append(True)

    engine.register(
        HookSpec(event=HookEvent.POST_TOOL_USE, handler=fast, timeout_ms=500)
    )
    fire_ctx = HookContext(event=HookEvent.POST_TOOL_USE, session_id="test")
    engine.fire_and_forget(fire_ctx)
    done, cancelled = await drain_pending(timeout=2.0)

    assert completed == [True]
    assert (done, cancelled) == (1, 0)
    oks = [c for c in recorded if c["ok"]]
    assert len(oks) == 1
    assert oks[0]["event"] == HookEvent.POST_TOOL_USE.value


@pytest.mark.asyncio
async def test_fire_and_forget_no_timeout_runs_unwrapped(engine, ctx, recorded):
    """timeout_ms unset/zero → handler runs with no wait_for wrapper.

    A handler that sleeps past any nominal timeout still runs to completion
    because no timeout was declared.
    """
    completed: list[bool] = []

    async def slow_no_timeout(c: HookContext) -> None:
        await asyncio.sleep(0.05)
        completed.append(True)

    engine.register(
        HookSpec(event=HookEvent.POST_TOOL_USE, handler=slow_no_timeout, timeout_ms=0)
    )
    fire_ctx = HookContext(event=HookEvent.POST_TOOL_USE, session_id="test")
    engine.fire_and_forget(fire_ctx)
    done, cancelled = await drain_pending(timeout=2.0)

    # No timeout wrapper → the handler ran to completion.
    assert completed == [True]
    assert (done, cancelled) == (1, 0)
    oks = [c for c in recorded if c["ok"]]
    assert len(oks) == 1
    timeouts = [c for c in recorded if not c["ok"] and "timeout" in c["summary"]]
    assert timeouts == []
