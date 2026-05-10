"""Tests for the retry_utils module (Hermes B5)."""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.agent.error_classifier import ErrorCategory
from opencomputer.agent.retry_utils import (
    RetryExhausted,
    _backoff_delay,
    retry,
    with_retry,
)


class _FakeRateLimit(Exception):  # noqa: N818 — mimics SDK class name
    """Mimics anthropic.RateLimitError shape (status_code attribute)."""

    def __init__(self, msg: str = "429") -> None:
        super().__init__(msg)
        self.status_code = 429


class _FakeAuth(Exception):  # noqa: N818 — mimics SDK class name
    def __init__(self, msg: str = "401") -> None:
        super().__init__(msg)
        self.status_code = 401


@pytest.mark.asyncio
async def test_returns_value_on_first_success() -> None:
    calls = 0

    async def fn() -> int:
        nonlocal calls
        calls += 1
        return 42

    result = await retry(fn)
    assert result == 42
    assert calls == 1


@pytest.mark.asyncio
async def test_retries_on_retryable_then_succeeds() -> None:
    calls = 0
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    async def fn() -> int:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _FakeRateLimit()
        return 99

    result = await retry(
        fn, max_attempts=5, base_delay_s=0.01, jitter=0, sleep=fake_sleep
    )
    assert result == 99
    assert calls == 3
    # First attempt has zero delay (skipped); attempts 2 and 3 each get a backoff.
    assert len(sleeps) == 2
    assert sleeps[0] == pytest.approx(0.01)
    assert sleeps[1] == pytest.approx(0.02)  # exponential


@pytest.mark.asyncio
async def test_exhaustion_wraps_in_retry_exhausted() -> None:
    async def fake_sleep(d: float) -> None:
        return None

    async def fn() -> None:
        raise _FakeRateLimit()

    with pytest.raises(RetryExhausted) as ei:
        await retry(fn, max_attempts=3, base_delay_s=0.01, sleep=fake_sleep)
    assert isinstance(ei.value.__cause__, _FakeRateLimit)


@pytest.mark.asyncio
async def test_non_retryable_raises_immediately() -> None:
    calls = 0

    async def fake_sleep(d: float) -> None:
        return None

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise _FakeAuth()

    with pytest.raises(_FakeAuth):
        await retry(fn, max_attempts=5, sleep=fake_sleep)
    assert calls == 1  # auth errors don't retry


@pytest.mark.asyncio
async def test_custom_retryable_categories() -> None:
    """Caller can override default retryable set — e.g. retry only NETWORK."""
    calls = 0

    async def fake_sleep(d: float) -> None:
        return None

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise _FakeRateLimit()  # category=RATE_LIMITED

    # Only NETWORK is retryable per this caller — RATE_LIMITED falls through.
    with pytest.raises(_FakeRateLimit):
        await retry(
            fn,
            max_attempts=5,
            sleep=fake_sleep,
            retryable_categories=frozenset({ErrorCategory.NETWORK}),
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_on_attempt_callback_fires_per_failure() -> None:
    seen: list[tuple[int, str, ErrorCategory]] = []

    async def fake_sleep(d: float) -> None:
        return None

    async def fn() -> None:
        raise _FakeRateLimit()

    def cb(attempt: int, exc: BaseException, category: ErrorCategory) -> None:
        seen.append((attempt, type(exc).__name__, category))

    with pytest.raises(RetryExhausted):
        await retry(fn, max_attempts=3, sleep=fake_sleep, on_attempt=cb)
    assert len(seen) == 3
    assert all(s[2] is ErrorCategory.RATE_LIMITED for s in seen)


@pytest.mark.asyncio
async def test_on_attempt_callback_swallows_its_own_errors() -> None:
    """A buggy on_attempt callback must not break the retry loop."""
    calls = 0

    async def fake_sleep(d: float) -> None:
        return None

    async def fn() -> int:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise _FakeRateLimit()
        return 1

    def cb(attempt: int, exc: BaseException, category: ErrorCategory) -> None:
        raise RuntimeError("buggy callback")

    result = await retry(fn, max_attempts=3, sleep=fake_sleep, on_attempt=cb)
    assert result == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_max_attempts_validation() -> None:
    async def fn() -> None:
        return None

    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        await retry(fn, max_attempts=0)


@pytest.mark.asyncio
async def test_decorator_form() -> None:
    calls = 0

    async def fake_sleep(d: float) -> None:
        return None

    @with_retry(max_attempts=3, base_delay_s=0)
    async def the_func() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise _FakeRateLimit()
        return "ok"

    # Decorator can't pass `sleep` so we just rely on base=0 + jitter still tiny.
    result = await the_func()
    assert result == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_args_kwargs_forwarded() -> None:
    seen: list[tuple[tuple, dict]] = []

    async def fake_sleep(d: float) -> None:
        return None

    async def fn(a: int, b: int, *, c: int) -> int:
        seen.append(((a, b), {"c": c}))
        return a + b + c

    result = await retry(fn, 1, 2, c=3, sleep=fake_sleep)
    assert result == 6
    assert seen == [((1, 2), {"c": 3})]


def test_backoff_delay_zero_for_first_attempt() -> None:
    assert _backoff_delay(1, base=1.0, cap=30.0, jitter=0) == 0.0


def test_backoff_delay_exponential() -> None:
    # No jitter — deterministic.
    assert _backoff_delay(2, base=1.0, cap=30.0, jitter=0) == 1.0
    assert _backoff_delay(3, base=1.0, cap=30.0, jitter=0) == 2.0
    assert _backoff_delay(4, base=1.0, cap=30.0, jitter=0) == 4.0


def test_backoff_delay_capped() -> None:
    assert _backoff_delay(20, base=1.0, cap=5.0, jitter=0) == 5.0


def test_backoff_delay_jitter_in_range() -> None:
    for _ in range(50):
        d = _backoff_delay(3, base=1.0, cap=30.0, jitter=0.5)
        # base 2.0 ± 50% → [1.0, 3.0]
        assert 1.0 <= d <= 3.0
