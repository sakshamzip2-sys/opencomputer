"""Phase 12b1 / Sub-project A Task A1 — cron-mode guard on MemoryBridge.

Background:
  Honcho is becoming the default memory provider. Some workflows (cron
  batches, periodic flushes) should NOT spin up the external provider —
  they're quick background work where the baseline SQLite+FTS5 is
  sufficient and a Docker stack would be overhead.

  Hermes uses this same pattern at
  ``sources/hermes-agent/plugins/memory/honcho/__init__.py:279-286``
  where ``agent_context in {"cron","flush"}`` skips the provider.

This task adds:
  * ``RuntimeContext.agent_context`` — a str field defaulting to ``"chat"``
    accepting ``"chat" | "cron" | "flush" | "review"`` (not enforced at
    construction time, lint-checked).
  * An optional ``runtime`` kwarg on ``MemoryBridge.prefetch`` that
    short-circuits to ``None`` when ``agent_context`` is ``"cron"`` or
    ``"flush"``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from opencomputer.agent.memory_bridge import MemoryBridge
from plugin_sdk.runtime_context import RuntimeContext


class _ExplodingProvider:
    """A fake MemoryProvider whose ``prefetch`` MUST NOT be called.

    If the bridge's cron/flush guard works correctly, this provider's
    ``prefetch`` will never be awaited, so the test can prove the guard
    short-circuited.
    """

    provider_id = "exploding-test-provider"

    async def prefetch(
        self, query: str, turn_index: int
    ) -> str | None:  # pragma: no cover
        raise AssertionError(
            "provider.prefetch should not have been called in cron/flush mode"
        )

    async def sync_turn(
        self, user: str, assistant: str, turn_index: int
    ) -> None:  # pragma: no cover
        raise AssertionError("sync_turn should not be called in this test")

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    def tool_schemas(self) -> list:  # pragma: no cover
        return []

    async def handle_tool_call(self, call: Any) -> Any:  # pragma: no cover
        return None


class _FakeMemoryContext:
    """Minimal stand-in for ``MemoryContext`` used by ``MemoryBridge``.

    The bridge only reads ``.provider`` and ``._failure_state`` off the
    context — duck typing is enough.
    """

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self._failure_state: dict[str, Any] = {}


@pytest.mark.asyncio
async def test_memory_bridge_skips_provider_in_cron_context() -> None:
    """When ``agent_context="cron"``, the bridge returns None without
    calling the provider."""
    provider = _ExplodingProvider()
    ctx = _FakeMemoryContext(provider)
    bridge = MemoryBridge(ctx)

    runtime = RuntimeContext(agent_context="cron")
    result = await bridge.prefetch("any query", turn_index=0, runtime=runtime)

    assert result is None


@pytest.mark.asyncio
async def test_memory_bridge_skips_provider_in_flush_context() -> None:
    """When ``agent_context="flush"``, the bridge returns None without
    calling the provider."""
    provider = _ExplodingProvider()
    ctx = _FakeMemoryContext(provider)
    bridge = MemoryBridge(ctx)

    runtime = RuntimeContext(agent_context="flush")
    result = await bridge.prefetch("any query", turn_index=0, runtime=runtime)

    assert result is None


@pytest.mark.asyncio
async def test_memory_bridge_calls_provider_in_default_chat_context() -> None:
    """Default ``agent_context="chat"`` (and runtime=None) still hits the
    provider — the guard must not over-reach."""

    class _RecordingProvider:
        provider_id = "recording-test-provider"

        def __init__(self) -> None:
            self.prefetch_mock = AsyncMock(return_value="from-provider")

        async def prefetch(self, query: str, turn_index: int) -> str | None:
            return await self.prefetch_mock(query, turn_index)

    # Case A: explicit chat runtime
    provider_a = _RecordingProvider()
    bridge_a = MemoryBridge(_FakeMemoryContext(provider_a))
    result_a = await bridge_a.prefetch(
        "hello", turn_index=0, runtime=RuntimeContext(agent_context="chat")
    )
    assert result_a == "from-provider"
    provider_a.prefetch_mock.assert_awaited_once_with("hello", 0)

    # Case B: no runtime at all (backwards compat — existing callers)
    provider_b = _RecordingProvider()
    bridge_b = MemoryBridge(_FakeMemoryContext(provider_b))
    result_b = await bridge_b.prefetch("hi", turn_index=1)
    assert result_b == "from-provider"
    provider_b.prefetch_mock.assert_awaited_once_with("hi", 1)


@pytest.mark.asyncio
async def test_memory_bridge_sync_turn_skips_provider_in_cron_context() -> None:
    """Symmetric with prefetch: cron turns that complete must not
    ``provider.sync_turn`` on the way out — otherwise the guard only covers
    read and leaks on write."""
    provider = _ExplodingProvider()
    ctx = _FakeMemoryContext(provider)
    bridge = MemoryBridge(ctx)

    runtime = RuntimeContext(agent_context="cron")
    # Must not raise — _ExplodingProvider.sync_turn would AssertionError if called.
    await bridge.sync_turn("user msg", "assistant reply", turn_index=0, runtime=runtime)


@pytest.mark.asyncio
async def test_memory_bridge_sync_turn_calls_provider_in_chat_context() -> None:
    """Default chat context still syncs — the guard must not over-reach."""

    class _RecordingProvider:
        provider_id = "recording-test-provider"

        def __init__(self) -> None:
            self.sync_mock = AsyncMock(return_value=None)

        async def prefetch(
            self, query: str, turn_index: int
        ) -> str | None:  # pragma: no cover
            return None

        async def sync_turn(self, user: str, assistant: str, turn_index: int) -> None:
            await self.sync_mock(user, assistant, turn_index)

    provider = _RecordingProvider()
    bridge = MemoryBridge(_FakeMemoryContext(provider))
    await bridge.sync_turn(
        "u", "a", turn_index=3, runtime=RuntimeContext(agent_context="chat")
    )
    provider.sync_mock.assert_awaited_once_with("u", "a", 3)
