"""Wave 3 — stream-stall watchdog."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from opencomputer.agent.stream_watchdog import stream_with_watchdog
from plugin_sdk import BaseProvider, StreamStaleException


async def _quick_stream() -> AsyncIterator[int]:
    for i in range(3):
        yield i
        await asyncio.sleep(0)


async def _slow_stream() -> AsyncIterator[int]:
    yield 0
    await asyncio.sleep(60.0)
    yield 1  # never reached when watchdog fires


@pytest.mark.asyncio
async def test_watchdog_passes_through_when_disabled():
    chunks = []
    async for chunk in stream_with_watchdog(
        _quick_stream(), stale_timeout_seconds=None, provider_name="x"
    ):
        chunks.append(chunk)
    assert chunks == [0, 1, 2]


@pytest.mark.asyncio
async def test_watchdog_passes_through_quick_stream():
    chunks = []
    async for chunk in stream_with_watchdog(
        _quick_stream(), stale_timeout_seconds=5.0, provider_name="x"
    ):
        chunks.append(chunk)
    assert chunks == [0, 1, 2]


@pytest.mark.asyncio
async def test_watchdog_fires_on_stalled_stream():
    chunks = []
    with pytest.raises(StreamStaleException) as exc_info:
        async for chunk in stream_with_watchdog(
            _slow_stream(), stale_timeout_seconds=0.05, provider_name="testprov"
        ):
            chunks.append(chunk)
    assert chunks == [0]
    assert exc_info.value.provider_name == "testprov"
    assert exc_info.value.stale_seconds == pytest.approx(0.05, rel=0.1)


def test_baseprovider_default_request_timeout():
    """All BaseProvider subclasses inherit the 60.0s default."""

    class _Stub(BaseProvider):
        async def complete(self, **kw):  # type: ignore[override]
            return None

        async def stream_complete(self, **kw):  # type: ignore[override]
            yield None

    p = _Stub()
    assert p.request_timeout_seconds == 60.0
    assert p.stale_timeout_seconds is None


def test_baseprovider_subclass_can_override_timeouts():
    class _Slow(BaseProvider):
        request_timeout_seconds = 300.0
        stale_timeout_seconds = 30.0

        async def complete(self, **kw):  # type: ignore[override]
            return None

        async def stream_complete(self, **kw):  # type: ignore[override]
            yield None

    p = _Slow()
    assert p.request_timeout_seconds == 300.0
    assert p.stale_timeout_seconds == 30.0


def test_streamstaleexception_message_format():
    exc = StreamStaleException("my-provider", 12.5)
    assert "my-provider" in str(exc)
    assert "12.5s" in str(exc)
