"""Per-profile state for screen-awareness — opt-in flags + tunables.

State lives at ``<profile_home>/screen_awareness_state.json``. Default
is fully disabled. Mirrors ambient-sensors's AmbientState pattern.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

#: State file basename — placed under profile_home.
_STATE_FILENAME = "screen_awareness_state.json"


@dataclass(frozen=True, slots=True)
class ScreenAwarenessState:
    """Per-profile screen-awareness configuration."""

    enabled: bool = False  # master switch — must be True for any capture
    persist: bool = False  # opt-in JSONL append log
    cooldown_seconds: float = 1.0
    ring_size: int = 20
    freshness_seconds: float = 60.0
    max_chars: int = 4_000


def _state_path(profile_home: Path) -> Path:
    return profile_home / _STATE_FILENAME


def load_state(profile_home: Path) -> ScreenAwarenessState:
    """Load state from disk; return default if missing or corrupt."""
    path = _state_path(profile_home)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ScreenAwarenessState()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ScreenAwarenessState()
    if not isinstance(data, dict):
        return ScreenAwarenessState()
    valid_fields = ScreenAwarenessState.__dataclass_fields__.keys()
    clean = {k: v for k, v in data.items() if k in valid_fields}
    try:
        return ScreenAwarenessState(**clean)
    except TypeError:
        return ScreenAwarenessState()


def save_state(profile_home: Path, state: ScreenAwarenessState) -> None:
    """Atomic write — temp file + rename."""
    profile_home.mkdir(parents=True, exist_ok=True)
    path = _state_path(profile_home)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    os.replace(tmp, path)


__all__ = ["ScreenAwarenessState", "load_state", "save_state"]
