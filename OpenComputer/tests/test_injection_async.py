"""Tests that InjectionEngine.collect_all runs providers in parallel (IV.1 refactor).

The IV.1 refactor converts ``DynamicInjectionProvider.collect`` from ``def`` to
``async def`` and updates ``InjectionEngine`` so all providers run via
``asyncio.gather``. This file pins the two invariants that matter:

1. Providers run concurrently — two 100ms sleeps must complete in ~100ms
   total, not ~200ms.
2. A single provider raising is tolerated — its contribution drops, the
   surviving providers still contribute.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from opencomputer.agent.injection import InjectionEngine
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT


class _SleepingProvider(DynamicInjectionProvider):
    """Test double — sleeps for ``delay_s`` inside ``collect()`` then returns ``text``."""

    def __init__(self, text: str, delay_s: float, provider_id: str, priority: int = 50):
        self._text = text
        self._delay = delay_s
        self._provider_id = provider_id
        self._priority = priority

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def priority(self) -> int:  # type: ignore[override]
        return self._priority

    async def collect(self, ctx: InjectionContext) -> str | None:
        await asyncio.sleep(self._delay)
        return self._text


@pytest.mark.asyncio
async def test_injection_engine_runs_providers_in_parallel() -> None:
    """Two providers each sleeping 100ms should finish in ~100ms, not 200ms."""
    engine = InjectionEngine()
    engine.register(_SleepingProvider("A", 0.1, "a", priority=10))
    engine.register(_SleepingProvider("B", 0.1, "b", priority=20))

    ctx = InjectionContext(messages=(), runtime=DEFAULT_RUNTIME_CONTEXT)

    start = time.monotonic()
    result = await engine.collect_all(ctx)
    elapsed = time.monotonic() - start

    # Generous margin: parallel should be ~0.1s; serial would be ~0.2s.
    # 0.15s leaves room for scheduler jitter on a loaded CI box.
    assert elapsed < 0.15, (
        f"expected parallel (~0.1s), got {elapsed:.3f}s — providers likely ran serially"
    )
    assert result == ["A", "B"], f"expected ordered contributions, got {result!r}"


@pytest.mark.asyncio
async def test_injection_engine_survives_provider_exception() -> None:
    """A provider that raises should not crash the turn; its contribution just drops."""

    class _BrokenProvider(DynamicInjectionProvider):
        priority = 50

        @property
        def provider_id(self) -> str:
            return "broken"

        async def collect(self, ctx: InjectionContext) -> str | None:
            raise RuntimeError("boom")

    engine = InjectionEngine()
    engine.register(_SleepingProvider("ok", 0.01, "a", priority=10))
    engine.register(_BrokenProvider())

    ctx = InjectionContext(messages=(), runtime=DEFAULT_RUNTIME_CONTEXT)
    result = await engine.collect_all(ctx)

    # Surviving provider's contribution must still appear. Broken one drops.
    assert result == ["ok"], f"expected ['ok'], got {result!r}"


@pytest.mark.asyncio
async def test_injection_engine_preserves_deterministic_order() -> None:
    """Even under parallel gather, results must come back in (priority, provider_id) order.

    Otherwise the aggregate system-prompt string would shift between turns,
    breaking prompt caching on the LLM side — which is the whole point of
    the deterministic sort in the pre-refactor engine.
    """
    engine = InjectionEngine()
    # Register in reverse of the expected output order to prove sorting happens.
    engine.register(_SleepingProvider("third", 0.01, "c", priority=90))
    engine.register(_SleepingProvider("first", 0.01, "a", priority=10))
    engine.register(_SleepingProvider("second", 0.01, "b", priority=50))

    ctx = InjectionContext(messages=(), runtime=DEFAULT_RUNTIME_CONTEXT)
    result = await engine.collect_all(ctx)
    assert result == ["first", "second", "third"], (
        f"expected priority-asc order, got {result!r}"
    )


@pytest.mark.asyncio
async def test_injection_engine_compose_is_async_and_joins() -> None:
    """``compose`` is the convenience wrapper for ``collect_all`` + join."""
    engine = InjectionEngine()
    engine.register(_SleepingProvider("A", 0.01, "a", priority=10))
    engine.register(_SleepingProvider("B", 0.01, "b", priority=20))

    ctx = InjectionContext(messages=(), runtime=DEFAULT_RUNTIME_CONTEXT)
    out = await engine.compose(ctx)
    assert out == "A\n\nB"
