"""Per-target activity tracking.

Module-level dict keyed by ``target_id`` (or any opaque scope key).
Stores ``time.monotonic()`` of the last action — used by the gateway
for "browser status" output (when did the agent last act?) and for
idle-detection in sandbox sessions.

Process-lifetime only — no disk persistence (matches OpenClaw).
``clear_activity()`` is exposed for profile-reset paths.
"""

from __future__ import annotations

import time
from typing import Final

_last_action_time: Final[dict[str, float]] = {}


def record_action(target_id: str) -> None:
    """Stamp the current monotonic time for ``target_id``."""
    if not target_id:
        return
    _last_action_time[target_id] = time.monotonic()


def last_action_time(target_id: str) -> float | None:
    """Return the most recent monotonic timestamp, or ``None`` if never."""
    if not target_id:
        return None
    return _last_action_time.get(target_id)


def seconds_since_last_action(target_id: str) -> float | None:
    """Convenience: how long since the last action, in seconds."""
    t = last_action_time(target_id)
    if t is None:
        return None
    return max(0.0, time.monotonic() - t)


def clear_activity(target_id: str | None = None) -> None:
    """Drop activity for one target (or all, if ``target_id`` is None)."""
    if target_id is None:
        _last_action_time.clear()
        return
    _last_action_time.pop(target_id, None)


def known_target_ids() -> list[str]:
    return list(_last_action_time.keys())
