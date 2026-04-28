"""Tests for BaseChannelAdapter._send_with_retry (Hermes PR 2 Task 2.1).

Exponential backoff with jitter for transient send errors. Non-retryable
errors propagate immediately. Hermes parity (gateway/platforms/base.py).
"""

from __future__ import annotations

import pytest

from plugin_sdk.channel_contract import BaseChannelAdapter
from plugin_sdk.core import Platform, SendResult


class _FakeAdapter(BaseChannelAdapter):
    platform = Platform.CLI

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, text, **kwargs):
        return SendResult(success=True)


@pytest.mark.asyncio
async def test_send_with_retry_first_try_success() -> None:
    adapter = _FakeAdapter({})
    calls: list[int] = []

    async def fn(*a, **kw):
        calls.append(1)
        return SendResult(success=True)

    res = await adapter._send_with_retry(fn, "chat", "text")
    assert res.success
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_send_with_retry_retries_on_retryable() -> None:
    adapter = _FakeAdapter({})
    calls: list[int] = []

    async def fn(*a, **kw):
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("connection reset by peer")
        return SendResult(success=True)

    res = await adapter._send_with_retry(fn, "chat", "text", base_delay=0.01)
    assert res.success
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_send_with_retry_does_not_retry_timeout() -> None:
    """Read/write timeouts are non-idempotent — never retried automatically."""
    adapter = _FakeAdapter({})
    calls: list[int] = []

    async def fn(*a, **kw):
        calls.append(1)
        raise TimeoutError("read timed out")

    with pytest.raises(TimeoutError):
        await adapter._send_with_retry(fn, "chat", "text", base_delay=0.01)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_send_with_retry_exhausts_returns_failure_result() -> None:
    adapter = _FakeAdapter({})
    calls: list[int] = []

    async def fn(*a, **kw):
        calls.append(1)
        raise ConnectionError("network unreachable")

    res = await adapter._send_with_retry(
        fn, "chat", "text", max_attempts=3, base_delay=0.01
    )
    assert not res.success
    assert "network" in (res.error or "").lower()
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_send_with_retry_passes_args_and_kwargs() -> None:
    adapter = _FakeAdapter({})
    seen: dict = {}

    async def fn(chat_id, text, *, parse_mode=None):
        seen["chat_id"] = chat_id
        seen["text"] = text
        seen["parse_mode"] = parse_mode
        return SendResult(success=True)

    await adapter._send_with_retry(fn, "c", "hi", parse_mode="MarkdownV2")
    assert seen == {"chat_id": "c", "text": "hi", "parse_mode": "MarkdownV2"}


@pytest.mark.asyncio
async def test_send_with_retry_propagates_non_retryable() -> None:
    """ValueError isn't transient; do not retry, propagate."""
    adapter = _FakeAdapter({})
    calls: list[int] = []

    async def fn(*a, **kw):
        calls.append(1)
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        await adapter._send_with_retry(fn, "chat", "text", base_delay=0.01)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_is_retryable_error_classes() -> None:
    adapter = _FakeAdapter({})
    assert adapter._is_retryable_error(ConnectionError("connection reset"))
    assert adapter._is_retryable_error(OSError("network unreachable"))
    assert not adapter._is_retryable_error(ValueError("bad input"))
    assert not adapter._is_retryable_error(TimeoutError("read timed out"))
    # ConnectTimeout-style names with "connect" + "timeout" are retryable
    # (connect-time transient — never reached the server).
    class ConnectTimeoutError(Exception):
        pass

    assert adapter._is_retryable_error(ConnectTimeoutError("connect timed out"))
