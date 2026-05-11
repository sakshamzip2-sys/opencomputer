"""Tests for opencomputer/agent/stream_retry.py.

Covers four surfaces:

1. ``RetryPolicy`` construction validates inputs (raises early).
2. ``is_pre_stream_transient`` classifier (provider-agnostic
   string matching, with explicit exclusions for 429/auth).
3. ``compute_backoff_seconds`` curve (attempt=1 immediate, exponential
   growth, cap, equal-jitter range, deterministic with seeded RNG).
4. ``stream_with_retry`` async-generator wrapper end-to-end:
   success, retry-then-success, exhaustion, mid-stream NO-retry,
   non-transient NO-retry, cancellation propagation, callback safety.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from opencomputer.agent.stream_retry import (
    DEFAULT_POLICY,
    RetryPolicy,
    RetryStatus,
    compute_backoff_seconds,
    is_pre_stream_transient,
    stream_with_retry,
)


# Minimal stand-in for plugin_sdk.provider_contract.StreamEvent so the
# tests don't depend on the full provider import surface. The wrapper
# treats events as opaque payloads — only the wrapper code reads
# event.kind / event.text, and the tests assert on identity.
@dataclass(frozen=True, slots=True)
class _Ev:
    kind: str
    text: str = ""


# ─── RetryPolicy validation ────────────────────────────────────────────


class TestRetryPolicy:
    def test_defaults_are_valid(self) -> None:
        p = RetryPolicy()
        assert p.max_attempts == 4
        assert p.base_delay_seconds == 0.75
        assert p.cap_delay_seconds == 8.0
        assert p.jitter_ratio == 0.5

    def test_custom_values_valid(self) -> None:
        p = RetryPolicy(
            max_attempts=3,
            base_delay_seconds=1.0,
            cap_delay_seconds=10.0,
            jitter_ratio=0.0,
        )
        assert p.max_attempts == 3
        assert p.base_delay_seconds == 1.0
        assert p.cap_delay_seconds == 10.0
        assert p.jitter_ratio == 0.0

    @pytest.mark.parametrize("bad", [0, -1, -100])
    def test_max_attempts_floor(self, bad: int) -> None:
        with pytest.raises(ValueError, match="max_attempts must be >= 1"):
            RetryPolicy(max_attempts=bad)

    def test_max_attempts_ceiling(self) -> None:
        with pytest.raises(ValueError, match="max_attempts > 16"):
            RetryPolicy(max_attempts=17)

    def test_max_attempts_type_check(self) -> None:
        with pytest.raises(TypeError, match="max_attempts must be int"):
            RetryPolicy(max_attempts=4.0)  # type: ignore[arg-type]

    def test_max_attempts_rejects_bool(self) -> None:
        # ``bool`` is a subclass of ``int`` in Python — the explicit
        # ``isinstance(..., bool)`` check guards against this footgun.
        with pytest.raises(TypeError, match="max_attempts must be int"):
            RetryPolicy(max_attempts=True)  # type: ignore[arg-type]

    def test_base_delay_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="base_delay_seconds must be >= 0"):
            RetryPolicy(base_delay_seconds=-0.5)

    def test_cap_below_base_rejected(self) -> None:
        with pytest.raises(ValueError, match="cap_delay_seconds .* must be >="):
            RetryPolicy(base_delay_seconds=5.0, cap_delay_seconds=1.0)

    def test_cap_too_large_rejected(self) -> None:
        with pytest.raises(ValueError, match="cap_delay_seconds > 60s"):
            RetryPolicy(cap_delay_seconds=120.0)

    @pytest.mark.parametrize("bad", [-0.1, 1.01, 2.0, -1.0])
    def test_jitter_ratio_out_of_range(self, bad: float) -> None:
        with pytest.raises(ValueError, match=r"jitter_ratio must be in \[0\.0, 1\.0\]"):
            RetryPolicy(jitter_ratio=bad)

    def test_jitter_zero_and_one_allowed(self) -> None:
        RetryPolicy(jitter_ratio=0.0)
        RetryPolicy(jitter_ratio=1.0)

    def test_dataclass_is_frozen(self) -> None:
        p = RetryPolicy()
        with pytest.raises((AttributeError, TypeError)):
            p.max_attempts = 7  # type: ignore[misc]


# ─── classifier ────────────────────────────────────────────────────────


class TestIsPreStreamTransient:
    @pytest.mark.parametrize(
        "msg",
        [
            "overloaded",
            "Overloaded",
            "overloaded_error",
            "{'type': 'error', 'error': {'type': 'overloaded_error', "
            "'message': 'Overloaded'}, 'request_id': 'req_xxx'}",
            "HTTP 503 service_unavailable",
            "HTTP 502 bad gateway",
            "HTTP 504 gateway timeout",
            "HTTP 500 internal_server_error",
            "Connection refused by upstream",
            "Connection reset by peer",
            "Remote end closed connection",
            "Read timeout after 60s",
            "Connect timeout (httpx)",
            "SSL handshake failure",
            "service temporarily unavailable",
        ],
    )
    def test_positive_cases(self, msg: str) -> None:
        assert is_pre_stream_transient(RuntimeError(msg)) is True

    @pytest.mark.parametrize(
        "msg",
        [
            "rate_limit_error: too many requests",
            "HTTP 429 rate limit exceeded",
            "rate limit hit",
            "HTTP 401 authentication_error",
            "HTTP 403 permission_error",
            "HTTP 404 not_found_error",
            "invalid_request_error: missing model",
            "bad input schema",
        ],
    )
    def test_explicit_exclusions(self, msg: str) -> None:
        assert is_pre_stream_transient(RuntimeError(msg)) is False

    def test_cancellation_never_transient(self) -> None:
        assert is_pre_stream_transient(asyncio.CancelledError("overloaded")) is False

    def test_generator_exit_never_transient(self) -> None:
        assert is_pre_stream_transient(GeneratorExit()) is False

    def test_keyboard_interrupt_never_transient(self) -> None:
        assert is_pre_stream_transient(KeyboardInterrupt()) is False

    def test_empty_exception_falls_back_to_typename(self) -> None:
        class OverloadedError(Exception):
            pass

        # Empty args → classifier falls back to type name; "overloaded"
        # is in the type name, so this matches.
        assert is_pre_stream_transient(OverloadedError()) is True

    def test_empty_exception_no_typename_match(self) -> None:
        class MyError(Exception):
            pass

        # Empty args + non-matching type name → not transient.
        assert is_pre_stream_transient(MyError()) is False

    def test_unrelated_error_not_transient(self) -> None:
        assert is_pre_stream_transient(ValueError("bad schema")) is False
        assert is_pre_stream_transient(KeyError("missing key")) is False

    # ─── structural status_code defense ────────────────────────────

    def test_status_code_429_excluded_even_without_message_marker(self) -> None:
        """anthropic.RateLimitError surfaces as str ``"Too many requests"``
        with no "429" / "rate_limit" substring — the structural
        ``exc.status_code == 429`` check is what excludes it.
        """

        class _RateLimitedError(Exception):
            status_code = 429

        e = _RateLimitedError("Too many requests")
        assert is_pre_stream_transient(e) is False

    def test_status_code_too_many_requests_string_defense(self) -> None:
        """Bare RuntimeError without status_code attribute — string
        defense catches the message form.
        """
        assert is_pre_stream_transient(RuntimeError("Too many requests")) is False

    def test_status_code_401_403_404_excluded(self) -> None:
        for code in (401, 403, 404):

            class _StatusError(Exception):
                status_code = code

            assert is_pre_stream_transient(_StatusError("any message")) is False, code

    @pytest.mark.parametrize("code", [500, 502, 503, 504, 529, 599])
    def test_status_code_5xx_included(self, code: int) -> None:
        """5xx status codes are all retryable — including Anthropic's
        idiosyncratic 529 ``overloaded_error``.
        """

        class _StatusCodedError(Exception):
            pass

        e = _StatusCodedError("opaque message")
        e.status_code = code
        assert is_pre_stream_transient(e) is True, code

    def test_status_code_4xx_other_excluded(self) -> None:
        """4xx codes outside the explicit set (400/422/etc.) are NOT
        retryable — they're permanent client errors.
        """
        for code in (400, 422, 418):

            class _StatusCodedError(Exception):
                pass

            e = _StatusCodedError("any")
            e.status_code = code
            assert is_pre_stream_transient(e) is False, code

    def test_status_code_non_int_falls_through_to_string(self) -> None:
        """A non-int status_code is ignored — string-scan decides."""

        class _WeirdError(Exception):
            status_code = "not-a-number"

        # Falls through; "overloaded" in message → True.
        assert is_pre_stream_transient(_WeirdError("HTTP 529 overloaded_error")) is True
        # Falls through; no transient marker → False.
        assert is_pre_stream_transient(_WeirdError("oops")) is False


# ─── backoff curve ─────────────────────────────────────────────────────


class TestComputeBackoffSeconds:
    def test_first_attempt_is_immediate(self) -> None:
        assert compute_backoff_seconds(1, RetryPolicy()) == 0.0

    def test_attempt_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="attempt must be >= 1"):
            compute_backoff_seconds(0, RetryPolicy())
        with pytest.raises(ValueError, match="attempt must be >= 1"):
            compute_backoff_seconds(-5, RetryPolicy())

    def test_attempt_type_check(self) -> None:
        with pytest.raises(TypeError, match="attempt must be int"):
            compute_backoff_seconds(2.0, RetryPolicy())  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="attempt must be int"):
            compute_backoff_seconds(True, RetryPolicy())  # type: ignore[arg-type]

    def test_no_jitter_is_deterministic(self) -> None:
        p = RetryPolicy(
            max_attempts=8,
            base_delay_seconds=1.0,
            cap_delay_seconds=16.0,
            jitter_ratio=0.0,
        )
        # base * 2^(attempt-2): 1, 2, 4, 8, 16, 16, 16
        assert compute_backoff_seconds(2, p) == 1.0
        assert compute_backoff_seconds(3, p) == 2.0
        assert compute_backoff_seconds(4, p) == 4.0
        assert compute_backoff_seconds(5, p) == 8.0
        assert compute_backoff_seconds(6, p) == 16.0
        assert compute_backoff_seconds(7, p) == 16.0  # capped
        assert compute_backoff_seconds(8, p) == 16.0  # still capped

    def test_jitter_centered_on_curve(self) -> None:
        p = RetryPolicy(
            max_attempts=4,
            base_delay_seconds=2.0,
            cap_delay_seconds=10.0,
            jitter_ratio=0.5,
        )
        # attempt=3 → curve=4.0, half-width = 4.0*0.5/2 = 1.0 → [3.0, 5.0]
        samples = [
            compute_backoff_seconds(3, p, rng=random.Random(seed))
            for seed in range(200)
        ]
        assert all(3.0 - 1e-9 <= s <= 5.0 + 1e-9 for s in samples)
        # With 200 samples, average should be near the center (4.0).
        avg = sum(samples) / len(samples)
        assert 3.5 < avg < 4.5

    def test_jitter_respects_cap(self) -> None:
        p = RetryPolicy(
            base_delay_seconds=0.5,
            cap_delay_seconds=1.0,
            jitter_ratio=1.0,
        )
        # attempt high enough that curve = cap; jitter ratio 1.0 →
        # could push above cap if not clamped.
        for seed in range(100):
            v = compute_backoff_seconds(6, p, rng=random.Random(seed))
            assert 0.0 <= v <= 1.0

    def test_seeded_rng_is_reproducible(self) -> None:
        p = RetryPolicy(jitter_ratio=0.4)
        v1 = compute_backoff_seconds(3, p, rng=random.Random(42))
        v2 = compute_backoff_seconds(3, p, rng=random.Random(42))
        assert v1 == v2

    def test_default_policy_curve_bounded(self) -> None:
        # Sanity: all attempts within the default policy stay below cap.
        for attempt in range(1, DEFAULT_POLICY.max_attempts + 1):
            v = compute_backoff_seconds(
                attempt, DEFAULT_POLICY, rng=random.Random(0)
            )
            assert 0.0 <= v <= DEFAULT_POLICY.cap_delay_seconds


# ─── stream_with_retry ─────────────────────────────────────────────────


async def _events(*items: _Ev) -> AsyncIterator[_Ev]:
    """Helper to materialise events as an async iterator."""
    for it in items:
        yield it


async def _raise_after(items: list[_Ev], exc: BaseException) -> AsyncIterator[_Ev]:
    """Yield items, then raise — to test mid-stream failures."""
    for it in items:
        yield it
    raise exc


async def _raise_immediately(exc: BaseException) -> AsyncIterator[_Ev]:
    """Raise on first __anext__ without yielding — pre-first-byte fail."""
    if False:
        yield _Ev("noop")  # pragma: no cover — make this an async gen
    raise exc


class _FakeProvider:
    """Records calls and replays a queued sequence of behaviors per attempt.

    Each entry in ``script`` is either:
      * ``[StreamEvent, ...]``  → yield those then end (success)
      * ``Exception``           → raise immediately on first __anext__
      * ``(["mid-event"], Exception)`` → yield event(s) then raise
    """

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.script:
            raise RuntimeError(
                f"FakeProvider exhausted at call {self.calls}; the test "
                "asked for more attempts than scripted"
            )
        behavior = self.script.pop(0)
        if isinstance(behavior, BaseException):
            return _raise_immediately(behavior)
        if isinstance(behavior, tuple):
            mid, exc = behavior
            return _raise_after(list(mid), exc)
        return _events(*behavior)


_FAST_POLICY = RetryPolicy(
    max_attempts=4,
    base_delay_seconds=0.0,
    cap_delay_seconds=0.0,
    jitter_ratio=0.0,
)
"""Zero-delay variant for tests — keeps the suite fast while exercising
every code path. ``cap=0`` is fine because ``base=0`` and the validator
only requires ``cap >= base``."""


async def _no_sleep(_: float) -> None:
    """Drop-in replacement for asyncio.sleep that returns immediately."""
    return None


class TestStreamWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        provider = _FakeProvider(
            [
                [
                    _Ev("text_delta", "hello"),
                    _Ev("text_delta", " world"),
                    _Ev("done"),
                ]
            ]
        )
        events = []
        async for ev in stream_with_retry(
            provider, policy=_FAST_POLICY, sleep=_no_sleep
        ):
            events.append(ev)
        assert provider.calls == 1
        assert [e.text for e in events if e.kind == "text_delta"] == [
            "hello",
            " world",
        ]

    @pytest.mark.asyncio
    async def test_retry_then_success(self) -> None:
        provider = _FakeProvider(
            [
                RuntimeError("HTTP 529 overloaded_error: Overloaded"),
                [_Ev("text_delta", "ok"), _Ev("done")],
            ]
        )
        statuses: list[RetryStatus] = []
        events = []
        async for ev in stream_with_retry(
            provider,
            policy=_FAST_POLICY,
            sleep=_no_sleep,
            retry_callback=statuses.append,
        ):
            events.append(ev)
        assert provider.calls == 2
        assert [e.text for e in events if e.kind == "text_delta"] == ["ok"]
        assert len(statuses) == 1
        assert statuses[0].attempt == 1
        assert statuses[0].next_attempt == 2
        assert statuses[0].exhausted is False
        assert statuses[0].error_kind == "overloaded"

    @pytest.mark.asyncio
    async def test_exhaustion_propagates_last_error(self) -> None:
        provider = _FakeProvider(
            [
                RuntimeError("HTTP 529 overloaded_error: spike-1"),
                RuntimeError("HTTP 502 bad gateway: spike-2"),
                RuntimeError("HTTP 503 service_unavailable: spike-3"),
                RuntimeError("HTTP 529 overloaded_error: spike-4"),
            ]
        )
        statuses: list[RetryStatus] = []
        with pytest.raises(RuntimeError, match="spike-4"):
            async for _ in stream_with_retry(
                provider,
                policy=_FAST_POLICY,
                sleep=_no_sleep,
                retry_callback=statuses.append,
            ):
                pass
        assert provider.calls == 4
        # 3 inter-attempt statuses + 1 exhausted status = 4 total.
        assert len(statuses) == 4
        assert [s.exhausted for s in statuses] == [False, False, False, True]
        assert statuses[-1].error_message.endswith("spike-4")

    @pytest.mark.asyncio
    async def test_mid_stream_failure_does_not_retry(self) -> None:
        provider = _FakeProvider(
            [
                (
                    [_Ev("text_delta", "partial")],
                    RuntimeError("overloaded_error mid-flight"),
                ),
                # If retry erroneously fires we'd consume this; the
                # test asserts we DON'T reach it.
                [_Ev("text_delta", "should-not-emit"), _Ev("done")],
            ]
        )
        statuses: list[RetryStatus] = []
        events = []
        with pytest.raises(RuntimeError, match="overloaded_error mid-flight"):
            async for ev in stream_with_retry(
                provider,
                policy=_FAST_POLICY,
                sleep=_no_sleep,
                retry_callback=statuses.append,
            ):
                events.append(ev)
        # Exactly one attempt, the partial event flowed to caller.
        assert provider.calls == 1
        assert [e.text for e in events if e.kind == "text_delta"] == ["partial"]
        # Mid-stream raise does NOT fire the retry callback — the
        # wrapper propagates without entering retry logic.
        assert statuses == []

    @pytest.mark.asyncio
    async def test_non_transient_pre_stream_does_not_retry(self) -> None:
        provider = _FakeProvider(
            [
                RuntimeError("HTTP 401 authentication_error: bad key"),
                [_Ev("text_delta", "should-not-emit")],
            ]
        )
        statuses: list[RetryStatus] = []
        with pytest.raises(RuntimeError, match="authentication_error"):
            async for _ in stream_with_retry(
                provider,
                policy=_FAST_POLICY,
                sleep=_no_sleep,
                retry_callback=statuses.append,
            ):
                pass
        assert provider.calls == 1
        assert statuses == []

    @pytest.mark.asyncio
    async def test_cancellation_propagates_immediately(self) -> None:
        provider = _FakeProvider(
            [
                # Yield one event then raise CancelledError mid-stream.
                (
                    [_Ev("text_delta", "x")],
                    asyncio.CancelledError(),
                ),
                # Should never be reached.
                [_Ev("text_delta", "should-not-emit")],
            ]
        )
        with pytest.raises(asyncio.CancelledError):
            async for _ in stream_with_retry(
                provider, policy=_FAST_POLICY, sleep=_no_sleep
            ):
                pass
        assert provider.calls == 1

    @pytest.mark.asyncio
    async def test_pre_stream_cancellation_propagates(self) -> None:
        provider = _FakeProvider(
            [
                asyncio.CancelledError(),
                # Should never be reached.
                [_Ev("text_delta", "should-not-emit")],
            ]
        )
        with pytest.raises(asyncio.CancelledError):
            async for _ in stream_with_retry(
                provider, policy=_FAST_POLICY, sleep=_no_sleep
            ):
                pass
        assert provider.calls == 1

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_wedge_retry(self) -> None:
        provider = _FakeProvider(
            [
                RuntimeError("overloaded_error: spike"),
                [_Ev("text_delta", "recovered"), _Ev("done")],
            ]
        )
        invocations = 0

        def _bad_callback(status: RetryStatus) -> None:
            nonlocal invocations
            invocations += 1
            raise RuntimeError("ui blew up")

        events = []
        async for ev in stream_with_retry(
            provider,
            policy=_FAST_POLICY,
            sleep=_no_sleep,
            retry_callback=_bad_callback,
        ):
            events.append(ev)
        assert provider.calls == 2
        assert invocations == 1
        assert [e.text for e in events if e.kind == "text_delta"] == [
            "recovered"
        ]

    @pytest.mark.asyncio
    async def test_factory_raises_synchronously_classifies(self) -> None:
        """If the factory itself raises (rare), the classifier still applies."""

        attempts = 0

        def _factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("HTTP 503 service_unavailable")
            return _events(_Ev("text_delta", "ok"), _Ev("done"))

        events = []
        async for ev in stream_with_retry(
            _factory, policy=_FAST_POLICY, sleep=_no_sleep
        ):
            events.append(ev)
        assert attempts == 2

    @pytest.mark.asyncio
    async def test_factory_raises_non_transient_propagates(self) -> None:
        def _factory():
            raise RuntimeError("HTTP 401 authentication_error")

        with pytest.raises(RuntimeError, match="authentication_error"):
            async for _ in stream_with_retry(
                _factory, policy=_FAST_POLICY, sleep=_no_sleep
            ):
                pass

    @pytest.mark.asyncio
    async def test_factory_must_be_callable(self) -> None:
        with pytest.raises(TypeError, match="factory must be callable"):
            # Need to actually iterate to trigger validation since
            # stream_with_retry is an async generator function.
            async for _ in stream_with_retry("not-callable"):  # type: ignore[arg-type]
                pass

    @pytest.mark.asyncio
    async def test_max_attempts_one_disables_retry(self) -> None:
        provider = _FakeProvider(
            [
                RuntimeError("overloaded_error: spike"),
                [_Ev("text_delta", "should-not-emit")],
            ]
        )
        single_attempt = RetryPolicy(
            max_attempts=1, base_delay_seconds=0.0, cap_delay_seconds=0.0
        )
        statuses: list[RetryStatus] = []
        with pytest.raises(RuntimeError, match="spike"):
            async for _ in stream_with_retry(
                provider,
                policy=single_attempt,
                sleep=_no_sleep,
                retry_callback=statuses.append,
            ):
                pass
        assert provider.calls == 1
        # max_attempts=1 → no inter-attempt status, just the exhausted one.
        assert len(statuses) == 1
        assert statuses[0].exhausted is True

    @pytest.mark.asyncio
    async def test_sleep_actually_invoked(self) -> None:
        provider = _FakeProvider(
            [
                RuntimeError("overloaded_error: spike"),
                [_Ev("text_delta", "ok"), _Ev("done")],
            ]
        )
        sleeps: list[float] = []

        async def _record_sleep(d: float) -> None:
            sleeps.append(d)

        policy = RetryPolicy(
            max_attempts=4,
            base_delay_seconds=0.25,
            cap_delay_seconds=1.0,
            jitter_ratio=0.0,
        )
        async for _ in stream_with_retry(
            provider, policy=policy, sleep=_record_sleep
        ):
            pass
        # One retry → one sleep, with the policy's deterministic value
        # for attempt=2 (base * 2^0 = 0.25, no jitter).
        assert sleeps == [0.25]

    @pytest.mark.asyncio
    async def test_no_callback_is_fine(self) -> None:
        """Wrapper works correctly when retry_callback is None."""
        provider = _FakeProvider(
            [
                RuntimeError("overloaded_error: spike"),
                [_Ev("text_delta", "ok"), _Ev("done")],
            ]
        )
        events = []
        async for ev in stream_with_retry(
            provider, policy=_FAST_POLICY, sleep=_no_sleep
        ):
            events.append(ev)
        assert provider.calls == 2
        assert [e.text for e in events if e.kind == "text_delta"] == ["ok"]

    @pytest.mark.asyncio
    async def test_classification_in_status(self) -> None:
        """error_kind tag reflects the exception shape correctly."""
        provider = _FakeProvider(
            [
                RuntimeError("HTTP 502 bad gateway"),
                RuntimeError("HTTP 503 service_unavailable"),
                RuntimeError("HTTP 504 gateway timeout"),
                RuntimeError("ssl handshake failed"),
            ]
        )
        statuses: list[RetryStatus] = []
        with pytest.raises(RuntimeError):
            async for _ in stream_with_retry(
                provider,
                policy=_FAST_POLICY,
                sleep=_no_sleep,
                retry_callback=statuses.append,
            ):
                pass
        kinds = [s.error_kind for s in statuses]
        assert kinds == [
            "bad_gateway",
            "service_unavailable",
            "gateway_timeout",
            "tls",
        ]

    @pytest.mark.asyncio
    async def test_long_error_message_truncated(self) -> None:
        long = "x" * 5000
        provider = _FakeProvider(
            [
                RuntimeError(f"HTTP 503 service_unavailable: {long}"),
                [_Ev("text_delta", "ok"), _Ev("done")],
            ]
        )
        statuses: list[RetryStatus] = []
        async for _ in stream_with_retry(
            provider,
            policy=_FAST_POLICY,
            sleep=_no_sleep,
            retry_callback=statuses.append,
        ):
            pass
        assert len(statuses) == 1
        assert len(statuses[0].error_message) <= 200
        assert statuses[0].error_message.endswith("…")
