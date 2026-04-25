"""Smart model fallback routing (G.31 / Tier 4).

Helper for ``AgentLoop._run_one_step`` that retries a provider call
against a chain of fallback models when the primary fails with a
transient error (429 rate limit, 5xx upstream failure, connection
refused). Returns the first successful result or re-raises the last
error after exhausting the chain.

Streaming fallback is NOT in scope here — once the user has seen any
tokens, the loop is committed to that model. Streaming fallback is a
separate concern (would need to buffer the first event, then commit).
This helper is for the non-streaming path only.

Error detection is string-based (not subclass-based) because each
provider plugin is free to raise its own exception type — we don't
want to import provider-specific exceptions into core. Mirrors the
existing auth-failure heuristic in
``extensions/anthropic-provider/provider.py:282``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger("opencomputer.agent.fallback")


_TRANSIENT_MARKERS: tuple[str, ...] = (
    " 429",
    "rate_limit",
    "rate limit",
    " 500",
    " 502",
    " 503",
    " 504",
    "overloaded",
    "service_unavailable",
    "connection refused",
    "connection reset",
    "timed out",
    "timeout",
)
"""Substrings checked against the lowercased exception message. Each
marker is intentionally narrow: ``" 429"`` requires a leading space so
``code-429-day-special`` doesn't false-positive. ``rate_limit`` covers
both Anthropic and OpenAI's structured error codes."""


def is_transient_error(exc: BaseException) -> bool:
    """Return ``True`` if ``exc`` looks like a transient provider failure.

    Used to decide whether the fallback loop should retry against the
    next model in the chain. Conservative: when in doubt, return
    ``False`` and let the error surface — better to fail fast than to
    retry an unrecoverable error against three more models and waste
    quota.
    """
    msg = (str(exc) or "").lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


async def call_with_fallback[T](
    call: Callable[[str], Awaitable[T]],
    primary_model: str,
    fallback_models: tuple[str, ...],
) -> T:
    """Run ``call(model)`` against the primary, then each fallback in turn.

    Stops at the first success. Re-raises the LAST error (not the
    first) on full chain exhaustion so the caller sees the most-recent
    diagnostic. Non-transient errors short-circuit immediately —
    fallback only triggers on rate limits / 5xx / connection failures.

    Empty ``fallback_models`` collapses to a plain ``await call(primary_model)``
    with zero overhead.
    """
    chain: tuple[str, ...] = (primary_model, *fallback_models)
    last_exc: BaseException | None = None
    for idx, model in enumerate(chain):
        try:
            return await call(model)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not is_transient_error(exc):
                # Non-transient — re-raise immediately. Don't burn the
                # rest of the chain on an auth error / bad request /
                # missing tool.
                raise
            if idx == len(chain) - 1:
                # Exhausted — fall through to the post-loop raise.
                logger.warning(
                    "model fallback chain exhausted (%d models tried) — "
                    "re-raising last transient error",
                    len(chain),
                )
                break
            next_model = chain[idx + 1]
            logger.info(
                "model %r hit transient error (%s); falling back to %r",
                model,
                type(exc).__name__,
                next_model,
            )
    # ``last_exc`` is always set by the time we get here — at minimum
    # the primary model raised. Re-raise without a cause chain because
    # we already logged the intermediate failures.
    assert last_exc is not None  # pragma: no cover
    raise last_exc


__all__ = ["call_with_fallback", "is_transient_error"]
