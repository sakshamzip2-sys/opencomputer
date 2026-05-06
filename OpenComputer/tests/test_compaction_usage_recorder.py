"""Tests for CompactionEngine.usage_recorder wiring (Hermes B4 follow-up).

CompactionEngine takes an optional ``usage_recorder`` callback. When set,
it is invoked with ``ProviderResponse.usage`` after each compaction
provider call so callers (the agent loop) can persist the cost into
``llm_calls``. Best-effort: a buggy callback must not break compaction.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from opencomputer.agent.compaction import CompactionConfig, CompactionEngine
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    Usage,
)


class _StubProvider(BaseProvider):
    """Provider that returns a deterministic usage blob from complete()."""

    name = "stub"
    default_model = "stub-1"

    def __init__(self) -> None:
        self.complete_calls = 0

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        self.complete_calls += 1
        return ProviderResponse(
            message=Message(role="assistant", content="summary text"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=2000, output_tokens=300),
        )

    async def stream_complete(self, **kwargs: Any):
        if False:
            yield

    async def count_tokens(self, **kwargs: Any) -> int:
        return 0


@pytest.mark.asyncio
async def test_summarize_invokes_recorder_with_usage() -> None:
    provider = _StubProvider()
    captured: list[Usage] = []

    def recorder(usage: Usage) -> None:
        captured.append(usage)

    engine = CompactionEngine(
        provider=provider,
        model="stub-1",
        config=CompactionConfig(),
        usage_recorder=recorder,
    )
    out = await engine._summarize([Message(role="user", content="hi")])  # noqa: SLF001
    assert "summary text" in out
    assert len(captured) == 1
    assert captured[0].input_tokens == 2000
    assert captured[0].output_tokens == 300


@pytest.mark.asyncio
async def test_summarize_works_without_recorder() -> None:
    """Default (no recorder) — no behavior change."""
    provider = _StubProvider()
    engine = CompactionEngine(
        provider=provider,
        model="stub-1",
        config=CompactionConfig(),
    )
    out = await engine._summarize([Message(role="user", content="hi")])  # noqa: SLF001
    assert out == "summary text"


@pytest.mark.asyncio
async def test_summarize_swallows_buggy_recorder() -> None:
    """A buggy callback must not break compaction (telemetry-best-effort)."""
    provider = _StubProvider()

    def buggy_recorder(usage: Usage) -> None:
        raise RuntimeError("boom")

    engine = CompactionEngine(
        provider=provider,
        model="stub-1",
        config=CompactionConfig(),
        usage_recorder=buggy_recorder,
    )
    # Should not raise
    out = await engine._summarize([Message(role="user", content="hi")])  # noqa: SLF001
    assert out == "summary text"


@pytest.mark.asyncio
async def test_recorder_receives_usage_per_call() -> None:
    """One call → one recording. Multiple calls → multiple recordings."""
    provider = _StubProvider()
    rec = MagicMock()
    engine = CompactionEngine(
        provider=provider, model="stub-1", config=CompactionConfig(),
        usage_recorder=rec,
    )
    await engine._summarize([Message(role="user", content="a")])  # noqa: SLF001
    await engine._summarize([Message(role="user", content="b")])  # noqa: SLF001
    assert rec.call_count == 2
