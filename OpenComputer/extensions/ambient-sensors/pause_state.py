"""Pause/resume state for the ambient sensor daemon.

State file at ``<profile_home>/ambient/state.json``. CLI writes; daemon reads
each tick. Default state (missing or corrupt file) is DISABLED — the
privacy-safe default for any user who hasn't explicitly opted in.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AmbientState:
    enabled: bool = False
    paused_until: float | None = None
    sensors: tuple[str, ...] = field(default_factory=tuple)


def load_state(path: Path) -> AmbientState:
    """Read state.json; return default (disabled) if missing/unreadable/corrupt."""
    if not path.exists():
        return AmbientState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return AmbientState()
    return AmbientState(
        enabled=bool(raw.get("enabled", False)),
        paused_until=raw.get("paused_until"),
        sensors=tuple(raw.get("sensors", ())),
    )


def save_state(path: Path, state: AmbientState) -> None:
    """Write state.json (pretty-printed JSON; creates parent dir)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "enabled": state.enabled,
                "paused_until": state.paused_until,
                "sensors": list(state.sensors),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def is_currently_paused(state: AmbientState) -> bool:
    """The daemon is "currently paused" iff:
    - enabled is True (otherwise the daemon shouldn't even be running), AND
    - paused_until is set AND in the future.
    """
    if not state.enabled:
        return False
    if state.paused_until is None:
        return False
    return state.paused_until > time.time()
