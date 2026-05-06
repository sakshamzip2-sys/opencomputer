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
"""

from __future__ import annotations

import logging
from typing import Any

from opencomputer.agent.state import SessionDB
from opencomputer.cost_guard.pricing import compute_call_cost

logger = logging.getLogger(__name__)


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


__all__ = ["record_call", "record_call_from_usage"]
