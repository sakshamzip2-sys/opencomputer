"""Per-call usage pricing — Hermes B4 wiring.

This module is the loop-side bridge between
:func:`opencomputer.cost_guard.pricing.compute_call_cost` (which knows
how to turn (model, tokens) into a USD figure) and
:meth:`opencomputer.agent.state.SessionDB.record_llm_call` (which knows
how to persist the result).

Why a separate module rather than inline code in ``loop.py``?
``loop.py`` is already 4k LOC; isolating this gives us a cheap
testable surface and lets future callers (cron jobs, batch APIs) reuse
the recording path without re-implementing.

Active-session ContextVars (Hermes-followup 2026-05-07)
-------------------------------------------------------

Auxiliary callers (title-gen daemon thread, judge-reviewer, dreaming,
recall-synthesizer, etc.) don't always have ``session_id`` + ``db`` in
their immediate scope. ``AgentLoop`` sets two ``ContextVar``s at the
start of ``run_conversation`` so any code running in that conversation's
context — including daemon threads spawned with ``copy_context()`` —
can read the active session and DB without signature changes:

- :data:`_active_session_id`: the live session id, or ``""`` outside a
  conversation.
- :data:`_active_db`: the live :class:`SessionDB`, or ``None``.

Aux callers should use :func:`record_response_in_active_session` which
reads both, swallows when neither is set, and records otherwise.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from opencomputer.agent.state import SessionDB
from opencomputer.cost_guard.pricing import compute_call_cost

logger = logging.getLogger(__name__)


# ─── active-session context ────────────────────────────────────────────────

#: The session id of the in-flight conversation. ``AgentLoop`` writes
#: this at ``run_conversation`` entry; ``""`` means "no active session".
_active_session_id: ContextVar[str] = ContextVar(
    "oc_active_session_id", default=""
)

#: The :class:`SessionDB` of the in-flight conversation. ``None`` outside.
_active_db: ContextVar[SessionDB | None] = ContextVar(
    "oc_active_db", default=None
)


def set_active_session(session_id: str, db: SessionDB) -> None:
    """Mark the active conversation. Idempotent — only the loop calls this."""
    _active_session_id.set(session_id)
    _active_db.set(db)


def clear_active_session() -> None:
    """Reset the active session — call after the conversation ends."""
    _active_session_id.set("")
    _active_db.set(None)


def active_session_id() -> str:
    """Return the active session id, or ``""`` when no conversation is in flight."""
    return _active_session_id.get()


def active_db() -> SessionDB | None:
    """Return the active SessionDB, or ``None`` when no conversation is in flight."""
    return _active_db.get()


def record_call(
    *,
    db: SessionDB,
    session_id: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    batch: bool = False,
) -> float | None:
    """Compute cost from token counts and persist into ``llm_calls``.

    Returns the computed ``cost_usd`` (or ``None`` if no pricing data
    was available — caller can log if desired). Recording happens
    regardless of whether the cost is known: a NULL cost row still
    contributes to call-count and token-volume rollups.

    Idempotency note: caller must ensure this is invoked exactly once
    per *successful* provider completion. The retry path in the agent
    loop raises before usage is read, so retries don't double-record.
    """
    cost = None
    try:
        cost = compute_call_cost(
            provider=provider,
            model=model,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            batch=batch,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "compute_call_cost raised; recording row with cost_usd=NULL"
        )
    db.record_llm_call(
        session_id=session_id,
        provider=provider,
        model=model,
        input_tokens=int(input_tokens or 0),
        output_tokens=int(output_tokens or 0),
        cost_usd=cost,
        batch=batch,
    )
    return cost


def record_call_from_usage(
    *,
    db: SessionDB,
    session_id: str,
    provider: str,
    model: str,
    usage: Any,
    batch: bool = False,
) -> float | None:
    """Convenience overload: read tokens from a provider ``usage`` blob.

    Accepts either a ``plugin_sdk.provider_contract.Usage`` dataclass
    (with ``input_tokens`` / ``output_tokens`` attrs) or a dict with
    those keys. Falls back to 0 for missing fields.
    """
    if usage is None:
        return None
    input_tokens = _coerce_int(_get(usage, "input_tokens"))
    output_tokens = _coerce_int(_get(usage, "output_tokens"))
    return record_call(
        db=db,
        session_id=session_id,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        batch=batch,
    )


def _get(blob: Any, key: str) -> Any:
    if isinstance(blob, dict):
        return blob.get(key)
    return getattr(blob, key, None)


def _coerce_int(v: Any) -> int:
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _provider_name(provider: Any) -> str:
    """Best-effort string id for a provider object.

    Mirrors the loop.py + compaction-recorder fallback so aux callers
    don't reimplement: prefer ``.name``, fall back to lowercased class
    name with ``provider`` suffix stripped.
    """
    name = getattr(provider, "name", None)
    if name:
        return str(name)
    return type(provider).__name__.lower().replace("provider", "")


def record_response_for_provider(
    *,
    provider: Any,
    model: str,
    response: Any,
    batch: bool = False,
) -> float | None:
    """One-line helper for auxiliary callers.

    Equivalent to::

        record_response_in_active_session(
            provider=_provider_name(provider),
            model=model,
            response=response,
            batch=batch,
        )

    Use this everywhere an auxiliary LLM call happens — title-gen,
    judge-reviewer, dreaming, recall-synthesizer, aux_llm, structured.
    Best-effort: no-op when no session is active.
    """
    return record_response_in_active_session(
        provider=_provider_name(provider),
        model=model,
        response=response,
        batch=batch,
    )


def record_response_in_active_session(
    *,
    provider: str,
    model: str,
    response: Any,
    batch: bool = False,
) -> float | None:
    """Record an aux-LLM call using the active-session ContextVars.

    Reads ``session_id`` + ``db`` from the active-session ContextVars.
    No-op when no session is active (e.g. a script imports an aux
    module and calls a provider directly outside a conversation —
    we don't want to crash or insert orphan rows).

    ``response`` is a :class:`plugin_sdk.provider_contract.ProviderResponse`
    or any object exposing ``.usage`` with ``.input_tokens`` /
    ``.output_tokens`` attributes.

    Best-effort: every exception is swallowed. Telemetry must not wedge
    the auxiliary call site.
    """
    sid = active_session_id()
    db = active_db()
    if not sid or db is None:
        return None
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    try:
        return record_call_from_usage(
            db=db,
            session_id=sid,
            provider=provider,
            model=model,
            usage=usage,
            batch=batch,
        )
    except Exception:  # noqa: BLE001
        logger.debug("record_response_in_active_session swallowed", exc_info=True)
        return None


__all__ = [
    "active_db",
    "active_session_id",
    "clear_active_session",
    "record_call",
    "record_call_from_usage",
    "record_response_for_provider",
    "record_response_in_active_session",
    "set_active_session",
]
