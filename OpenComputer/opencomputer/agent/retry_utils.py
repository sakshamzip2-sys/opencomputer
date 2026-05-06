"""Centralised async retry helper — Hermes parity port (B5).

Today, OpenComputer has retry logic in three places:

- ``CredentialPool.with_retry`` (provider-call retry tied to key rotation).
- Individual channel adapters (``_send_with_retry`` in
  ``plugin_sdk/channel_contract.py``).
- Ad-hoc per-caller try/except blocks across the loop and tools.

These don't duplicate each other (CredentialPool's loop is rotation-driven,
not backoff-driven), but they DO each implement their own backoff policy
and category-aware decision logic. This module is the single, opinionated
exponential-backoff retry primitive — callers reuse it instead of rolling
their own.

Usage
-----

Functional form::

    from opencomputer.agent.retry_utils import retry

    result = await retry(
        api_client.complete,
        prompt,
        max_attempts=3,
        base_delay_s=1.0,
    )

Decorator form::

    @with_retry(max_attempts=3)
    async def fetch_thing(url: str) -> bytes:
        ...

Category-aware retries: by default, only network/server/rate-limit/timeout
errors are retried (see :func:`opencomputer.agent.error_classifier.is_retryable`).
Auth and quota failures fall through immediately.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from opencomputer.agent.error_classifier import (
    ErrorCategory,
    classify,
    is_retryable,
)

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")

#: Default attempt count — total tries, not retries. ``max_attempts=3``
#: means: try once + retry twice on retryable errors.
DEFAULT_MAX_ATTEMPTS: int = 3
DEFAULT_BASE_DELAY_S: float = 1.0
DEFAULT_MAX_DELAY_S: float = 30.0
DEFAULT_JITTER: float = 0.5


class RetryExhausted(RuntimeError):  # noqa: N818 — name is the action, not the error type
    """Raised when all retry attempts have been used up.

    The original ``__cause__`` chains to the last underlying exception,
    so ``except RetryExhausted as e: raise e.__cause__`` works for
    callers that want the underlying error.
    """


def _backoff_delay(
    attempt: int,
    *,
    base: float,
    cap: float,
    jitter: float,
) -> float:
    """Compute the sleep delay before ``attempt`` (1-indexed).

    Exponential backoff: ``min(base * 2^(attempt-1), cap)`` with ±jitter%
    applied multiplicatively. Returns 0 for ``attempt <= 1`` (first try
    has no preceding delay).
    """
    if attempt <= 1:
        return 0.0
    raw = min(base * (2 ** (attempt - 2)), cap)
    if jitter > 0:
        scale = 1.0 + random.uniform(-jitter, jitter)
        raw = max(0.0, raw * scale)
    return raw


async def retry(  # noqa: UP047 — PEP 695 incompatible with ParamSpec usage
    fn: Callable[P, Awaitable[T]],
    *args: P.args,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    max_delay_s: float = DEFAULT_MAX_DELAY_S,
    jitter: float = DEFAULT_JITTER,
    retryable_categories: frozenset[ErrorCategory] | None = None,
    on_attempt: Callable[[int, BaseException, ErrorCategory], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    **kwargs: P.kwargs,
) -> T:
    """Call ``fn`` with retries on retryable errors.

    Parameters
    ----------
    fn:
        Coroutine function to invoke. Must be awaitable.
    *args, **kwargs:
        Forwarded to ``fn`` on every attempt.
    max_attempts:
        Total tries (default 3 — first call + two retries).
    base_delay_s, max_delay_s, jitter:
        Exponential backoff knobs. ``jitter`` is a multiplicative ratio
        in [0, 1] applied to the computed delay.
    retryable_categories:
        Override the default retryable set (rate-limit + timeout +
        network + server). Useful for callers that want narrower or
        broader semantics — e.g. ``frozenset({ErrorCategory.NETWORK})``
        to retry only transient network blips.
    on_attempt:
        Optional callback invoked after every failed attempt with
        ``(attempt_number, exception, category)``. Use to log or to
        feed metrics.
    sleep:
        Override for the sleep function (defaults to ``asyncio.sleep``).
        Tests inject a no-op or a recording sleeper.

    Raises
    ------
    The original exception, if it's not retryable.
    :class:`RetryExhausted`, if all retries fail. The ``__cause__`` is
    the last underlying exception.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    sleep_fn = sleep or asyncio.sleep
    last_exc: BaseException | None = None
    last_category: ErrorCategory = ErrorCategory.UNKNOWN

    for attempt in range(1, max_attempts + 1):
        delay = _backoff_delay(
            attempt, base=base_delay_s, cap=max_delay_s, jitter=jitter
        )
        if delay > 0:
            await sleep_fn(delay)
        try:
            return await fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            category = classify(exc) if isinstance(exc, Exception) else ErrorCategory.UNKNOWN
            last_exc = exc
            last_category = category
            if on_attempt is not None:
                try:
                    on_attempt(attempt, exc, category)
                except Exception:  # noqa: BLE001
                    logger.exception("retry on_attempt callback raised")
            should_retry = (
                isinstance(exc, Exception)
                and (
                    (retryable_categories is None and is_retryable(category))
                    or (
                        retryable_categories is not None
                        and category in retryable_categories
                    )
                )
            )
            if not should_retry or attempt >= max_attempts:
                if isinstance(exc, Exception) and should_retry:
                    # Exhausted — wrap so callers can distinguish "ran out of
                    # tries" from "first attempt failed fatally".
                    raise RetryExhausted(
                        f"Retry exhausted after {attempt} attempts "
                        f"(category={category.value})"
                    ) from exc
                raise

    # Defensive: should never reach here because the loop either returns or
    # raises. ``last_exc`` is non-None if we got here, but the type checker
    # doesn't know that.
    assert last_exc is not None
    raise RetryExhausted(
        f"Retry loop terminated without returning (category={last_category.value})"
    ) from last_exc


def with_retry(
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    max_delay_s: float = DEFAULT_MAX_DELAY_S,
    jitter: float = DEFAULT_JITTER,
    retryable_categories: frozenset[ErrorCategory] | None = None,
    on_attempt: Callable[[int, BaseException, ErrorCategory], None] | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Decorator form of :func:`retry`.

    Wraps a coroutine function so every call goes through the retry
    machinery with the configured parameters.
    """

    def _decorator(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(fn)
        async def _wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            return await retry(
                fn,
                *args,
                max_attempts=max_attempts,
                base_delay_s=base_delay_s,
                max_delay_s=max_delay_s,
                jitter=jitter,
                retryable_categories=retryable_categories,
                on_attempt=on_attempt,
                **kwargs,
            )

        return _wrapped

    return _decorator


__all__ = [
    "DEFAULT_BASE_DELAY_S",
    "DEFAULT_JITTER",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DELAY_S",
    "RetryExhausted",
    "retry",
    "with_retry",
]
