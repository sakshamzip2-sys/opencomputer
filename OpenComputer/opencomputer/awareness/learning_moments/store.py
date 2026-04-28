"""``learning_moments.json`` reader/writer with best-effort file lock.

State shape::

    {
        "version": 1,
        "moments_fired": {
            "memory_continuity_first_recall": 1714324800.0,
            "vibe_first_nonneutral": 1714411200.0
        },
        "fire_log": [
            {"id": "...", "fired_at": 1714324800.0}
        ],
        "first_reveal_appended": true
    }

Concurrent ``oc chat`` sessions are guarded by ``fcntl.flock`` on a
sibling lock file. Platforms without ``fcntl`` (Windows) fall through
and accept best-effort writes — worst case is one duplicate fire on a
same-day race, which is acceptable degradation.
"""
from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

_SCHEMA_VERSION = 1
_FIRE_LOG_RETENTION_SECONDS = 14 * 24 * 3600


@dataclass(slots=True)
class StoreState:
    """In-memory mirror of ``learning_moments.json``."""

    moments_fired: dict[str, float] = field(default_factory=dict)
    fire_log: list[dict] = field(default_factory=list)
    first_reveal_appended: bool = False


def _path(profile_home: Path) -> Path:
    return profile_home / "learning_moments.json"


def load(profile_home: Path) -> StoreState:
    """Return the store state, or an empty state if the file is missing
    or unreadable. Never raises."""
    p = _path(profile_home)
    if not p.exists():
        return StoreState()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return StoreState()
    if not isinstance(raw, dict):
        return StoreState()
    moments_fired = raw.get("moments_fired", {})
    if not isinstance(moments_fired, dict):
        moments_fired = {}
    fire_log = raw.get("fire_log", [])
    if not isinstance(fire_log, list):
        fire_log = []
    first = bool(raw.get("first_reveal_appended", False))
    return StoreState(
        moments_fired={
            str(k): float(v)
            for k, v in moments_fired.items()
            if isinstance(v, (int, float))
        },
        fire_log=[e for e in fire_log if isinstance(e, dict)],
        first_reveal_appended=first,
    )


def save(profile_home: Path, state: StoreState) -> None:
    """Write the state. Atomic via tmp+replace; best-effort flock."""
    p = _path(profile_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - _FIRE_LOG_RETENTION_SECONDS
    state.fire_log = [
        e for e in state.fire_log if float(e.get("fired_at", 0)) >= cutoff
    ]
    payload = {
        "version": _SCHEMA_VERSION,
        "moments_fired": state.moments_fired,
        "fire_log": state.fire_log,
        "first_reveal_appended": state.first_reveal_appended,
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    with contextlib.ExitStack() as cleanup:
        try:
            import fcntl
            lock_path = p.parent / ".learning_moments.lock"
            lock = open(lock_path, "w")  # noqa: SIM115
            cleanup.callback(lock.close)
            fcntl.flock(lock, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass  # platforms without fcntl: best-effort
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(p)


def seed_returning_user(profile_home: Path, total_sessions: int) -> None:
    """Idempotent seeding for users with prior sessions but no
    ``learning_moments.json`` yet.

    Threshold: 5 prior sessions. A user with that much history has
    already learned the agent's behavior implicitly; surfacing 3
    reveals on their next run would be noise, not value.
    """
    if total_sessions < 5:
        return
    if _path(profile_home).exists():
        return
    from opencomputer.awareness.learning_moments.registry import all_moments

    now = time.time()
    state = StoreState(
        moments_fired={m.id: now for m in all_moments()},
        first_reveal_appended=True,
    )
    save(profile_home, state)
