"""HookHistory — ring-buffer of recent hook fires.

Memory-only debug state (NOT audit state). Backs ``oc hooks list``
last-fired column + ``oc hooks clear``.

Thread-safe under the GIL for our use case (single deque per event;
append + iterate). For multi-process gateway daemons, history is
per-process — use the audit log for cross-process forensics.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass

_HISTORY_MAXLEN = 128
_SUMMARY_MAXLEN = 4096


@dataclass(frozen=True, slots=True)
class FireRecord:
    """One row of hook-fire history."""

    event: str
    source_id: str
    ts_utc: float
    ok: bool
    summary: str


_history: dict[str, deque[FireRecord]] = {}


def record_fire(event: str, source_id: str, *, ok: bool, summary: str) -> None:
    """Append a fire record for ``event``. Non-blocking; swallow exceptions
    so a buggy hook caller can't break the loop.
    """
    try:
        if len(summary) > _SUMMARY_MAXLEN:
            summary = summary[:_SUMMARY_MAXLEN] + "...[truncated]"
        rec = FireRecord(
            event=event,
            source_id=source_id,
            ts_utc=time.time(),
            ok=bool(ok),
            summary=summary,
        )
        bucket = _history.get(event)
        if bucket is None:
            bucket = deque(maxlen=_HISTORY_MAXLEN)
            _history[event] = bucket
        bucket.append(rec)
    except Exception:  # noqa: BLE001 — debug state must not crash the loop
        pass


def iter_history(event: str) -> Iterator[FireRecord]:
    """Iterate fire records for ``event`` (oldest → newest). Empty for
    unknown events.
    """
    bucket = _history.get(event)
    if bucket is None:
        return iter(())
    return iter(list(bucket))


def all_events() -> list[str]:
    """List events that have any history. Useful for `oc hooks list`."""
    return sorted(_history.keys())


def clear_history() -> int:
    """Clear all history. Returns count of records cleared."""
    n = sum(len(b) for b in _history.values())
    _history.clear()
    return n


__all__ = [
    "FireRecord",
    "record_fire",
    "iter_history",
    "all_events",
    "clear_history",
]
