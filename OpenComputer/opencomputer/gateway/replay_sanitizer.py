"""Replay sanitization for cold-start message catch-up (OpenClaw 1.D port).

When the gateway restarts after a network blip or process crash, buffered
messages may contain:
  - Stale assistant turns that were already streamed to the user (replay=True)
  - Outgoing-queue items still in flight (in_flight=True)
  - User messages older than ``max_age_seconds`` that are likely stale

``sanitize_for_replay`` drops these before re-feeding into Dispatch.

**Status:** the function is correct AGAINST messages that carry the right
markers. As of this PR, no writer in OC sets ``replay`` / ``in_flight`` on
messages — that's a deliberate scope split:

  - This PR ships the sanitizer + tests so the logic is reviewable in
    isolation.
  - A FOLLOW-UP PR adds schema columns + writer changes (gateway sets
    in_flight on enqueue/clears on ACK; dispatch sets replay=True on
    pre-shutdown buffered text). Until that lands, the function is a no-op
    for real Message rows (none have these markers).

  - The sanitizer is fully backwards-compatible: messages without the
    markers pass through unchanged.

Per AMENDMENTS Fix H6: the originally-planned single-PR scope is split
into two for safety. Schema migration is the heavier change.
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any


def sanitize_for_replay(
    messages: Iterable[Any],
    *,
    max_age_seconds: int = 300,
    now: float | None = None,
) -> list[Any]:
    """Drop stale / in-flight / over-aged messages from a replay batch.

    Accepts an iterable of message-like objects (dicts or attribute-bearing
    dataclasses). Each message is inspected for these markers:
      - ``replay`` (truthy) → drop (already-delivered assistant turn)
      - ``in_flight`` (truthy) → drop (outgoing-queue retry will deliver)
      - role == "user" AND ``ts`` < (now - max_age_seconds) → drop

    Messages without these markers pass through unchanged. Order is preserved
    for survivors.

    Args:
        messages: iterable of dicts OR objects with attribute-style access.
        max_age_seconds: drop user messages older than this many seconds.
        now: optional override for time.time() (used by tests).

    Returns:
        Filtered list of messages (copies of survivors; never mutates input).
    """
    cutoff = (now if now is not None else time.time()) - max_age_seconds
    out: list[Any] = []
    for m in messages:
        if _get(m, "replay"):
            continue
        if _get(m, "in_flight"):
            continue
        role = _get(m, "role")
        ts = _get(m, "ts")
        if role == "user" and isinstance(ts, (int, float)) and ts < cutoff:
            continue
        out.append(m)
    return out


def _get(m: Any, key: str) -> Any:
    """Read ``key`` from dict or attribute-style object; return None if absent."""
    if isinstance(m, dict):
        return m.get(key)
    return getattr(m, key, None)


__all__ = ["sanitize_for_replay"]
