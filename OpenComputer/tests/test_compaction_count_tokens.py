"""Tests for CompactionEngine.should_compact_now (Subsystem D follow-up)."""

from __future__ import annotations

from typing import Any

import pytest

from opencomputer.agent.compaction import CompactionConfig, CompactionEngine
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)


class _StubProvider(BaseProvider):
    """Stub provider with a configurable count_tokens response."""

    name = "stub"
    default_model = "stub-1"

    def __init__(self, fixed_token_count: int) -> None:
        self.fixed_token_count = fixed_token_count
        self.count_calls: int = 0

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        return ProviderResponse(
            message=Message(role="assistant", content="ok"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    async def stream_complete(self, **kwargs: Any):
        if False:
            yield

    async def count_tokens(self, **kwargs: Any) -> int:  # type: ignore[override]
        self.count_calls += 1
        return self.fixed_token_count


@pytest.mark.asyncio
async def test_should_compact_now_uses_provider_count(monkeypatch) -> None:
    """should_compact_now calls provider.count_tokens and applies threshold."""
    from opencomputer.agent import compaction as _comp

    # context_window_for(stub-1) is presumably small; force a known value.
    monkeypatch.setattr(_comp, "context_window_for", lambda m: 1000)
    provider = _StubProvider(fixed_token_count=900)  # 90% of 1000
    engine = CompactionEngine(
        provider=provider,
        model="stub-1",
        config=CompactionConfig(threshold_ratio=0.8),  # 80% threshold
    )

    # 900 / 1000 = 90% > 80% threshold → should compact
    assert await engine.should_compact_now([]) is True
    assert provider.count_calls == 1


@pytest.mark.asyncio
async def test_should_compact_now_below_threshold(monkeypatch) -> None:
    """Counts below threshold → don't compact."""
    from opencomputer.agent import compaction as _comp

    monkeypatch.setattr(_comp, "context_window_for", lambda m: 1000)
    provider = _StubProvider(fixed_token_count=400)  # 40% of 1000
    engine = CompactionEngine(
        provider=provider,
        model="stub-1",
        config=CompactionConfig(threshold_ratio=0.8),
    )

    assert await engine.should_compact_now([]) is False


@pytest.mark.asyncio
async def test_should_compact_now_returns_false_when_disabled(monkeypatch) -> None:
    """Disabled engines short-circuit before calling count_tokens."""
    from opencomputer.agent import compaction as _comp

    monkeypatch.setattr(_comp, "context_window_for", lambda m: 1000)
    provider = _StubProvider(fixed_token_count=999)
    engine = CompactionEngine(
        provider=provider,
        model="stub-1",
        config=CompactionConfig(threshold_ratio=0.8),
        disabled=True,
    )

    assert await engine.should_compact_now([]) is False
    assert provider.count_calls == 0  # short-circuited before provider call


@pytest.mark.asyncio
async def test_should_compact_now_returns_false_on_provider_error(monkeypatch) -> None:
    """If count_tokens raises, fall back to 'don't compact' (best-effort)."""
    from opencomputer.agent import compaction as _comp

    monkeypatch.setattr(_comp, "context_window_for", lambda m: 1000)

    class _FailingProvider(_StubProvider):
        async def count_tokens(self, **kwargs: Any) -> int:  # type: ignore[override]
            raise RuntimeError("network down")

    engine = CompactionEngine(
        provider=_FailingProvider(fixed_token_count=999),
        model="stub-1",
        config=CompactionConfig(threshold_ratio=0.8),
    )

    assert await engine.should_compact_now([]) is False
