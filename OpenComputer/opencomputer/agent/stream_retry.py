"""Pre-first-byte transient-failure retry for streaming provider calls.

The agent loop's non-streaming path uses :mod:`opencomputer.agent.fallback`
to walk a configured chain of fallback models on transient errors
(429/5xx/connection). The streaming path could not adopt the same helper
because once any token has been forwarded to the caller (``stream_callback``),
replaying the request would duplicate or contradict visible output.

But there is a narrow window where retry is safe: **before any event has
been yielded by the underlying provider stream**. Anthropic ``529
overloaded_error`` and most 5xx upstream failures land in exactly this
window — the API rejects the request entirely and the SSE stream never
opens. The first ``__anext__`` on the provider generator raises without
ever producing a ``text_delta``. Replaying that case is a no-op from the
user's perspective and frequently succeeds because 529 spikes typically
clear in seconds.

This module wraps any stream-iterator *factory* in a retry shell that:

  * forwards every event from a successful attempt to the caller;
  * on a pre-first-event raise that matches a small set of transient
    markers, sleeps according to a validated :class:`RetryPolicy` and
    re-invokes the factory to obtain a fresh stream;
  * NEVER retries once a single event has been yielded (mid-stream
    failure is propagated untouched);
  * NEVER retries on cancellation, ``GeneratorExit``, or
    ``KeyboardInterrupt``;
  * surfaces structured :class:`RetryStatus` events to an optional
    callback for UI rendering — callback exceptions are caught and
    logged at WARN so a misbehaving renderer never wedges the agent.

The module is intentionally provider-agnostic (string-based exception
classification, same convention as :mod:`opencomputer.agent.fallback`)
so it works for every provider plugin without importing their SDK
exception hierarchies into core.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugin_sdk.provider_contract import StreamEvent

__all__ = [
    "DEFAULT_POLICY",
    "RetryCallback",
    "RetryPolicy",
    "RetryStatus",
    "StreamFactory",
    "compute_backoff_seconds",
    "is_pre_stream_transient",
    "stream_with_retry",
]

_log = logging.getLogger("opencomputer.agent.stream_retry")


# ─── classifier ────────────────────────────────────────────────────────

_PRE_STREAM_TRANSIENT_MARKERS: tuple[str, ...] = (
    "overloaded_error",   # Anthropic 529 structured form
    "overloaded",         # generic 529 / "Overloaded" message
    " 502",
    " 503",
    " 504",
    " 500",
    "service_unavailable",
    "internal_server_error",
    "bad gateway",
    "gateway timeout",
    "connection refused",
    "connection reset",
    "connection aborted",
    "closed connection",     # http.client.RemoteDisconnected
    "server disconnected",   # httpx.RemoteProtocolError shape
    "remote disconnected",
    "remoteprotocolerror",
    "incomplete read",
    "temporarily unavailable",
    "temporary failure",
    "timed out",
    "read timeout",
    "connect timeout",
    "ssl",
)
"""Lowercased substrings checked against ``str(exc)``. Each marker is
intentionally narrow enough to avoid false positives but broad enough to
catch the common shapes of upstream + transport failures across
providers. Notable exclusions:

* ``429`` / ``rate_limit`` — handled by the provider's cross-session
  rate-guard (e.g. ``extensions/anthropic-provider/provider.py`` —
  ``_check_rate_limit`` / ``_record_429``). Retrying here would
  bypass the bucket and waste quota.
* ``401`` / ``403`` / ``404`` — auth / not-found errors are permanent,
  never transient.
* Pure ``"timeout"`` (without ``timed out`` / ``read timeout`` /
  ``connect timeout``) — too broad; many non-retryable failures
  surface "timeout" in their message (e.g. agent loop budget timeouts).
"""


# HTTP status codes that are NEVER pre-stream-transient. 429 lives
# here because rate-limit handling has its own cross-session bucket
# (e.g. ``extensions/anthropic-provider/provider.py``'s
# ``_check_rate_limit``); retrying through this layer would bypass the
# bucket and double-tax quota. 4xx auth / not-found are permanent.
_NON_TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({401, 403, 404, 429})


def is_pre_stream_transient(exc: BaseException) -> bool:
    """Return ``True`` if ``exc`` is a candidate for pre-first-byte retry.

    Conservative: when in doubt, return ``False`` so the error surfaces.
    Better to fail fast than to burn an entire backoff chain on an
    unrecoverable error (e.g. an invalid API key).

    Cancellation, ``GeneratorExit``, and ``KeyboardInterrupt`` always
    return ``False`` — they must propagate immediately.

    The classifier uses BOTH a structural check (``exc.status_code`` if
    present — most provider SDK exception classes expose this) AND a
    lowercased-substring scan of ``str(exc)``. The structural check is
    the primary defense for HTTP-status-coded errors; the string scan
    catches transports that surface only as a message (httpx ConnectError,
    asyncio TimeoutError, etc.). Belt-and-suspenders against a future
    provider SDK whose exception strings happen to alias a transient
    marker.

    Args:
        exc: any exception caught from the underlying stream.

    Returns:
        ``True`` when the wrapper should sleep + retry; ``False`` when
        the wrapper should re-raise immediately.
    """
    # NEVER retry control-flow / cancellation exceptions. Even if their
    # string representation happens to match a marker (paranoia), bail
    # out fast.
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit, KeyboardInterrupt)):
        return False
    # Structural check first — many provider SDKs (anthropic, openai)
    # expose ``status_code`` as an int on their exception classes.
    # ``getattr(..., default=None)`` returns None for non-HTTP exceptions
    # (TimeoutError, ConnectionError, etc.) so they fall through to the
    # string scan below.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in _NON_TRANSIENT_STATUS_CODES:
            return False
        # Anthropic specifically uses 529 for overloaded. 5xx as a class
        # are retryable upstream failures.
        if 500 <= status < 600:
            return True
        # 4xx (other than the explicit non-transient set above) — auth,
        # bad request, schema, etc. Not retryable here.
        if 400 <= status < 500:
            return False
    msg = str(exc).lower() if exc.args else type(exc).__name__.lower()
    # Explicit rate-limit / auth exclusion — string-form defense for
    # exceptions whose ``status_code`` isn't set but whose message
    # carries the diagnosis.
    if " 429" in msg or "rate_limit" in msg or "rate limit" in msg:
        return False
    if "too many requests" in msg:
        # anthropic.RateLimitError surfaces as ``"Too many requests"``
        # without "429" or "rate_limit" in str(exc); the structural
        # status_code=429 check above usually catches this, but the
        # string defense covers exceptions that drop the status (e.g.
        # bare ``RuntimeError("Too many requests")`` from a wrapper).
        return False
    if " 401" in msg or " 403" in msg or " 404" in msg:
        return False
    if "authentication_error" in msg or "permission_error" in msg:
        return False
    if "invalid_request_error" in msg or "not_found_error" in msg:
        return False
    return any(marker in msg for marker in _PRE_STREAM_TRANSIENT_MARKERS)


def _classify_error(exc: BaseException) -> str:
    """Return a short tag for telemetry / UI grouping.

    Pure best-effort categorization. Does NOT influence retry behavior
    — that is governed entirely by :func:`is_pre_stream_transient`.
    """
    msg = str(exc).lower() if exc.args else type(exc).__name__.lower()
    if "overloaded" in msg:
        return "overloaded"
    if " 503" in msg or "service_unavailable" in msg:
        return "service_unavailable"
    if " 502" in msg or "bad gateway" in msg:
        return "bad_gateway"
    if " 504" in msg or "gateway timeout" in msg:
        return "gateway_timeout"
    if " 500" in msg or "internal_server_error" in msg:
        return "internal_error"
    if "ssl" in msg or "tls" in msg:
        return "tls"
    if "connection" in msg:
        return "connection"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    return "transient"


_MAX_ERROR_MESSAGE_LEN = 200


def _short_error_message(exc: BaseException) -> str:
    """Truncate exception messages so the UI panel doesn't wrap awkwardly."""
    text = (str(exc) if exc.args else "") or type(exc).__name__
    if len(text) > _MAX_ERROR_MESSAGE_LEN:
        text = text[: _MAX_ERROR_MESSAGE_LEN - 1] + "…"
    return text


# ─── policy ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Backoff behavior for the streaming retry wrapper.

    All four fields are validated in ``__post_init__``. Constructing
    a ``RetryPolicy`` with invalid values raises immediately so config
    bugs surface at startup, not in the middle of a user turn.

    Defaults are calibrated for Anthropic ``529 overloaded_error``:

    * 4 total attempts (1 initial + 3 retries) — Anthropic's 529 spikes
      typically clear within 5–15 seconds; 4 attempts with the default
      curve give the cluster four chances within a 15-second total
      worst-case wait.
    * base 0.75s, cap 8.0s — short enough that the user isn't staring
      at an unresponsive terminal, long enough to actually let the
      upstream recover (the cap matters more than the base — without
      it exponential backoff grows past human-tolerance limits fast).
    * 0.5 equal jitter ratio — bounded perturbation reduces the chance
      of multiple sessions hitting the upstream in lockstep after a
      shared brownout, without making timing reasoning intractable.

    Attributes:
        max_attempts: total tries including the first. Must be ``>= 1``
            and ``<= 16``. ``1`` disables retry entirely (the wrapper
            still classifies + surfaces the first failure but doesn't
            sleep or replay).
        base_delay_seconds: seed for exponential backoff. Must be
            ``>= 0.0`` and ``<= cap_delay_seconds``.
        cap_delay_seconds: ceiling on any single sleep. Must be
            ``>= base_delay_seconds`` and ``<= 60.0`` (over a minute
            is user-hostile in an interactive terminal).
        jitter_ratio: full-width perturbation as a fraction of the
            capped value. Must be in ``[0.0, 1.0]``. ``0.0`` disables
            jitter (deterministic, useful for tests).
    """

    max_attempts: int = 4
    base_delay_seconds: float = 0.75
    cap_delay_seconds: float = 8.0
    jitter_ratio: float = 0.5

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int) or isinstance(
            self.max_attempts, bool
        ):
            raise TypeError(
                f"max_attempts must be int, got {type(self.max_attempts).__name__}"
            )
        if self.max_attempts < 1:
            raise ValueError(
                f"max_attempts must be >= 1, got {self.max_attempts}"
            )
        if self.max_attempts > 16:
            raise ValueError(
                f"max_attempts > 16 is unreasonable (got {self.max_attempts}); "
                "reduce or split via Config.fallback_models cross-model chain"
            )
        if not isinstance(self.base_delay_seconds, (int, float)) or isinstance(
            self.base_delay_seconds, bool
        ):
            raise TypeError(
                "base_delay_seconds must be float, got "
                f"{type(self.base_delay_seconds).__name__}"
            )
        if self.base_delay_seconds < 0.0:
            raise ValueError(
                f"base_delay_seconds must be >= 0.0, got {self.base_delay_seconds}"
            )
        if not isinstance(self.cap_delay_seconds, (int, float)) or isinstance(
            self.cap_delay_seconds, bool
        ):
            raise TypeError(
                "cap_delay_seconds must be float, got "
                f"{type(self.cap_delay_seconds).__name__}"
            )
        if self.cap_delay_seconds < self.base_delay_seconds:
            raise ValueError(
                f"cap_delay_seconds ({self.cap_delay_seconds}) must be >= "
                f"base_delay_seconds ({self.base_delay_seconds})"
            )
        if self.cap_delay_seconds > 60.0:
            raise ValueError(
                f"cap_delay_seconds > 60s (got {self.cap_delay_seconds}) "
                "would block the user for too long in an interactive terminal"
            )
        if not isinstance(self.jitter_ratio, (int, float)) or isinstance(
            self.jitter_ratio, bool
        ):
            raise TypeError(
                "jitter_ratio must be float, got "
                f"{type(self.jitter_ratio).__name__}"
            )
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError(
                f"jitter_ratio must be in [0.0, 1.0], got {self.jitter_ratio}"
            )


DEFAULT_POLICY: RetryPolicy = RetryPolicy()
"""Module-level default. Constructed once at import; safe to share
because :class:`RetryPolicy` is frozen and slots."""


def compute_backoff_seconds(
    attempt: int,
    policy: RetryPolicy,
    *,
    rng: random.Random | None = None,
) -> float:
    """Return seconds to sleep BEFORE attempt ``attempt`` (1-indexed).

    * ``attempt=1`` always returns ``0.0`` — the first attempt is
      immediate.
    * ``attempt>=2`` returns ``min(cap, base * 2 ** (attempt - 2))``
      with equal-jitter perturbation applied per
      :attr:`RetryPolicy.jitter_ratio`.

    The function is pure (modulo RNG) — same inputs produce the same
    output for the same RNG state. Tests can pass a seeded
    ``random.Random`` to assert exact values.

    Args:
        attempt: which attempt is about to start. Must be ``>= 1``.
        policy: governing :class:`RetryPolicy` (validated at
            construction).
        rng: optional ``random.Random`` for deterministic tests.
            When ``None``, uses the module-level :func:`random.random`.

    Returns:
        Non-negative float ``<= policy.cap_delay_seconds``.

    Raises:
        ValueError: if ``attempt < 1``.
    """
    if not isinstance(attempt, int) or isinstance(attempt, bool):
        raise TypeError(f"attempt must be int, got {type(attempt).__name__}")
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    if attempt == 1:
        return 0.0
    raw = policy.base_delay_seconds * (2 ** (attempt - 2))
    capped = min(raw, policy.cap_delay_seconds)
    if policy.jitter_ratio == 0.0 or capped == 0.0:
        return capped
    # Equal jitter: value uniformly drawn from a band centered on the
    # capped curve value, with half-width = capped * jitter_ratio / 2.
    half_width = capped * policy.jitter_ratio / 2.0
    sample = rng.random() if rng is not None else random.random()
    jittered = capped + (sample - 0.5) * 2.0 * half_width
    # Clamp to [0, cap] — jitter cannot push us negative or over cap.
    return max(0.0, min(jittered, policy.cap_delay_seconds))


# ─── status surface ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RetryStatus:
    """One status event surfaced to the optional retry callback.

    Emitted **between attempts** (after a failure, before the sleep
    completes) and **on exhaustion** (after the final attempt's
    failure, with ``exhausted=True``). The UI renders this as a
    transient line like ``Anthropic overloaded — retry 2/4 in 1.3s``.

    Attributes:
        attempt: 1-indexed number of the attempt that just failed.
        next_attempt: 1-indexed number of the attempt about to start.
            Equals ``attempt`` when ``exhausted`` is ``True`` (no
            further attempt will follow).
        max_attempts: total budget from the governing :class:`RetryPolicy`.
        delay_seconds: how long the wrapper will sleep before
            ``next_attempt``. ``0.0`` when ``exhausted=True``.
        error_kind: short, stable tag from :func:`_classify_error`
            (``"overloaded" | "bad_gateway" | "timeout" | ...``).
        error_message: truncated exception string for display.
        exhausted: ``True`` when this is the final status event before
            the wrapper re-raises the last error.
    """

    attempt: int
    next_attempt: int
    max_attempts: int
    delay_seconds: float
    error_kind: str
    error_message: str
    exhausted: bool


# ─── wrapper ───────────────────────────────────────────────────────────


StreamFactory = Callable[[], AsyncIterator["StreamEvent"]]
"""Zero-arg callable returning a fresh provider stream iterator per call.

The factory MUST be idempotent: calling it again must produce a brand
new iterator (no shared mutable state — fresh HTTP request, fresh
parser buffer, fresh watchdog). A factory that returns the same
already-exhausted generator on second call will deadlock the retry
loop.
"""

RetryCallback = Callable[[RetryStatus], None]
"""Synchronous callback invoked between attempts. Exceptions raised
inside the callback are caught + logged at WARNING and never propagate
into the stream loop."""


def _safe_invoke_callback(
    callback: RetryCallback | None, status: RetryStatus
) -> None:
    """Invoke the optional callback, swallowing any exception it raises."""
    if callback is None:
        return
    try:
        callback(status)
    except Exception as exc:  # noqa: BLE001 — UI bridge must never wedge the loop
        _log.warning(
            "stream_with_retry: retry_callback raised %s; ignoring "
            "(retry continues): %s",
            type(exc).__name__,
            exc,
        )


async def stream_with_retry(
    factory: StreamFactory,
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    retry_callback: RetryCallback | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
) -> AsyncIterator[StreamEvent]:
    """Async-generator wrapper that retries pre-first-byte transient failures.

    Behavior matrix:

    ============================  ===========================================
    Outcome                       Wrapper action
    ============================  ===========================================
    Stream completes               Returns normally (every event forwarded).
    Mid-stream raise               Propagates immediately (no retry — replay
                                   would duplicate visible content).
    Pre-first-byte non-transient   Propagates immediately (auth, 4xx, schema,
                                   etc. won't be fixed by retrying).
    Pre-first-byte transient,      Sleeps per :func:`compute_backoff_seconds`,
    attempts remaining             invokes ``retry_callback`` if provided,
                                   re-invokes ``factory()`` for a fresh stream.
    Pre-first-byte transient,      Invokes ``retry_callback`` with
    attempts exhausted             ``exhausted=True``, re-raises last error.
    Cancellation / GeneratorExit   Always propagates immediately.
    ============================  ===========================================

    Args:
        factory: zero-arg callable returning a fresh stream iterator
            per call. See :data:`StreamFactory`.
        policy: governing :class:`RetryPolicy` (default
            :data:`DEFAULT_POLICY`).
        retry_callback: optional sink for :class:`RetryStatus` events.
            Exceptions inside the callback are caught + logged; they
            never wedge the stream.
        sleep: injectable async sleep. Defaults to
            :func:`asyncio.sleep`. Tests pass a no-op to bypass real
            time.
        rng: optional ``random.Random`` for deterministic jitter in
            tests.

    Yields:
        :class:`StreamEvent` items in the order produced by the
        underlying provider.

    Raises:
        BaseException: re-raises whatever the underlying stream raised
            once retry is no longer applicable (mid-stream failure,
            non-transient pre-stream failure, or exhausted attempts).
    """
    if not callable(factory):
        raise TypeError(
            f"factory must be callable, got {type(factory).__name__}"
        )
    last_exc: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        # Step 1: construct the stream. The factory body should be
        # cheap — actual I/O happens inside the async generator. If the
        # factory itself raises (extremely unusual), classify the same
        # way as iteration-time failures.
        try:
            stream = factory()
        except (asyncio.CancelledError, GeneratorExit, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 — same classify-or-raise rule
            last_exc = exc
            stream = None
            if not is_pre_stream_transient(exc):
                _log.info(
                    "stream_with_retry: factory raised non-transient %s on "
                    "attempt %d/%d — propagating",
                    type(exc).__name__,
                    attempt,
                    policy.max_attempts,
                )
                raise
        # Step 2: drive the stream. Track whether any event has reached
        # the caller — that flips the retry switch off because any
        # further failure is post-emission and not safe to replay.
        emitted_any = False
        if stream is not None:
            try:
                async for event in stream:
                    emitted_any = True
                    yield event
            except (asyncio.CancelledError, GeneratorExit):
                # Cancellation: always propagate. Do NOT classify, do
                # NOT retry. The for-loop's __anext__ raised here; the
                # async generator runtime will drive aclose() on the
                # inner stream automatically.
                raise
            except Exception as exc:  # noqa: BLE001 — classify-or-raise
                last_exc = exc
                if emitted_any:
                    _log.info(
                        "stream_with_retry: mid-stream failure on attempt "
                        "%d/%d (%s) — propagating, retry not safe after "
                        "partial output",
                        attempt,
                        policy.max_attempts,
                        type(exc).__name__,
                    )
                    raise
                if not is_pre_stream_transient(exc):
                    _log.info(
                        "stream_with_retry: pre-stream failure on attempt "
                        "%d/%d is non-transient (%s) — propagating: %s",
                        attempt,
                        policy.max_attempts,
                        type(exc).__name__,
                        _short_error_message(exc),
                    )
                    raise
                # Fall through to retry decision.
            else:
                # Stream completed without raising — success.
                return
        # Step 3: retry decision. We reached here ONLY for a
        # pre-first-byte transient failure (factory raised transient,
        # or stream raised transient before yielding).
        if attempt >= policy.max_attempts:
            # Exhausted — surface the final status, then raise.
            assert last_exc is not None  # invariant: set above
            final_status = RetryStatus(
                attempt=attempt,
                next_attempt=attempt,
                max_attempts=policy.max_attempts,
                delay_seconds=0.0,
                error_kind=_classify_error(last_exc),
                error_message=_short_error_message(last_exc),
                exhausted=True,
            )
            _safe_invoke_callback(retry_callback, final_status)
            _log.warning(
                "stream_with_retry: exhausted %d attempts; final error "
                "(%s) propagates: %s",
                policy.max_attempts,
                type(last_exc).__name__,
                _short_error_message(last_exc),
            )
            raise last_exc
        next_attempt = attempt + 1
        delay = compute_backoff_seconds(next_attempt, policy, rng=rng)
        if last_exc is not None:
            inter_status = RetryStatus(
                attempt=attempt,
                next_attempt=next_attempt,
                max_attempts=policy.max_attempts,
                delay_seconds=delay,
                error_kind=_classify_error(last_exc),
                error_message=_short_error_message(last_exc),
                exhausted=False,
            )
            _safe_invoke_callback(retry_callback, inter_status)
            _log.info(
                "stream_with_retry: attempt %d/%d failed (%s); sleeping "
                "%.2fs then retrying",
                attempt,
                policy.max_attempts,
                _classify_error(last_exc),
                delay,
            )
        if delay > 0.0:
            await sleep(delay)
    # Unreachable: the loop either returns on success or raises on
    # exhaustion. The pragma keeps coverage honest while making the
    # invariant explicit to readers.
    raise AssertionError(  # pragma: no cover
        "stream_with_retry: control flow invariant violated — "
        "for-loop exited without return/raise"
    )
