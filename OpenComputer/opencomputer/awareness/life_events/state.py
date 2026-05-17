"""Per-profile ``life_event_state.json`` store — active life-event "teeth".

A small JSON knob file tracking which life-event patterns have currently
surfaced a hint to the user and which of those are awaiting a verdict on
the user's next reply. One entry per pattern::

    {
      "<pattern_id>": {
        "firing_ts": <float>,       # when the hint was surfaced
        "cron_id": "<str>",         # the scheduled follow-up's cron job id
        "surfaced": <bool>,         # the hint has been shown
        "verdict_pending": <bool>,  # the user's next reply judges this hint
        "surfaced_turn": <int>      # the turn index the hint surfaced on
      }
    }

``surfaced_turn`` is the 1-indexed turn number on which the hint was
injected into the prompt. The STOP-hook classifier compares the *current*
turn against it: only a STOP firing on a turn STRICTLY LATER than
``surfaced_turn`` judges the reply, because the user's reply to a hint
necessarily lands on the turn *after* the one the hint surfaced on. A
missing/zero ``surfaced_turn`` (pre-Task-7 entries) is treated as turn 0
so the next reply is always judged rather than ignored forever.

The file lives at ``<profile-home>/life_event_state.json`` — sibling of the
other per-profile knob files (``feature_flags.json``, ``cost_guard.json``,
…). Reads tolerate a missing or corrupt file (→ ``{}``); writes are atomic
(temp file in the same directory + ``os.replace``), matching
``FeatureFlagStore._atomic_write``.

Profile home is resolved via ``opencomputer.agent.config._home`` — the
canonical core resolver (ContextVar → ``OPENCOMPUTER_HOME`` → fallback).
``cli_awareness.py`` imports the same ``_home`` for ``muted_patterns.json``;
this module imports it from its core ``agent/config`` home rather than from
``cli_awareness`` to avoid an ``awareness/`` → ``cli_*`` backwards-layering
dependency. Resolved per call (not cached) so per-test ``OPENCOMPUTER_HOME``
monkey-patching picks up the right tmp path.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from opencomputer.profiles_lock import file_lock

_log = logging.getLogger(__name__)


def _state_path() -> Path:
    """Return the path to the per-profile ``life_event_state.json`` file.

    Resolved every call so tests that monkey-patch ``OPENCOMPUTER_HOME``
    per-test see the right tmp path.
    """
    from opencomputer.agent.config import _home

    return _home() / "life_event_state.json"


def _lock_path() -> Path:
    """Return the path to the per-profile ``.life_event_state.lock`` file.

    Mirrors :func:`_state_path` — resolved every call so per-test
    ``OPENCOMPUTER_HOME`` monkey-patching sees the right tmp path. The
    lock is a sibling dotfile of ``life_event_state.json``; it is kept
    separate because ``save_state`` atomic-replaces the json file, which
    would invalidate any flock held on the original inode.
    """
    from opencomputer.agent.config import _home

    return _home() / ".life_event_state.lock"


def load_state() -> dict:
    """Load the life-event state map. Tolerates a missing/corrupt file.

    Returns an empty dict — never raises — when the file is absent,
    unreadable, not valid JSON, or valid JSON that isn't an object.
    """
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("life_event_state.json read failed: %s; returning empty state", exc)
        return {}
    if not isinstance(data, dict):
        _log.warning(
            "life_event_state.json is not a dict (got %s); returning empty state",
            type(data).__name__,
        )
        return {}
    return data


def save_state(state: dict) -> None:
    """Persist the life-event state map atomically (truncate-then-write).

    Writes to a temp file in the target directory then ``os.replace``s it
    onto the destination, so a partially-written file is never observed.
    Mirrors ``FeatureFlagStore._atomic_write``.
    """
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=str(path.parent),
        delete=False,
        prefix=".life_event_state.",
        suffix=".tmp",
    ) as tmp:
        json.dump(state, tmp, indent=2)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


# Concurrency: the mutators below (mark_surfaced, clear,
# clear_verdict_pending) each do a non-atomic load→mutate→save. To stay
# correct when several surfaces write at once (e.g. gateway + CLI session
# concurrently), each mutator holds an exclusive advisory file lock
# (``.life_event_state.lock``, via ``file_lock``) across the WHOLE
# load→mutate→save sequence, so concurrent writers serialize instead of
# clobbering each other's updates. load_state / save_state /
# verdict_pending_patterns stay lock-free: they are reads, and save_state
# is already atomic via ``os.replace`` so a concurrent reader never sees a
# torn file.
def mark_surfaced(pattern_id: str, cron_id: str, surfaced_turn: int = 0) -> None:
    """Record (or overwrite) a freshly-surfaced life-event hint.

    Surfacing a hint ALWAYS makes the user's next reply verdict-pending
    (design spec §4.1), so the new entry carries ``surfaced=True`` and
    ``verdict_pending=True`` with ``firing_ts`` set to the current time.

    ``surfaced_turn`` is the 1-indexed turn number the hint surfaced on.
    The STOP-hook classifier uses it to skip the surfacing turn's own STOP
    (see module docstring). It is optional — defaulting to ``0`` — so
    existing callers and tests that don't thread a turn index keep working;
    a ``0`` simply means "always judge the next reply".

    The load→mutate→save runs under an exclusive file lock so concurrent
    writers on other surfaces cannot lose this update.
    """
    with file_lock(_lock_path()):
        state = load_state()
        state[pattern_id] = {
            "firing_ts": time.time(),
            "cron_id": cron_id,
            "surfaced": True,
            "verdict_pending": True,
            "surfaced_turn": int(surfaced_turn),
        }
        save_state(state)


def clear(pattern_id: str) -> None:
    """Remove a pattern's entry entirely. A missing pattern_id is a no-op.

    Used when a verdict refutes the hint — the whole tooth is dropped.
    Contrast with :func:`clear_verdict_pending`, which keeps the entry.

    The load→mutate→save runs under an exclusive file lock so concurrent
    writers on other surfaces cannot lose this update.
    """
    with file_lock(_lock_path()):
        state = load_state()
        if pattern_id in state:
            del state[pattern_id]
            save_state(state)


def clear_verdict_pending(pattern_id: str) -> None:
    """Flip a pattern's ``verdict_pending`` off, keeping the rest of the entry.

    Used when a verdict confirms (or is unclear about) the hint: the user's
    reply has been judged, so it is no longer verdict-pending, but the
    entry — and its ``cron_id`` — survive so the scheduled follow-up still
    fires. A missing pattern_id is a no-op (no raise).

    The load→mutate→save runs under an exclusive file lock so concurrent
    writers on other surfaces cannot lose this update.
    """
    with file_lock(_lock_path()):
        state = load_state()
        entry = state.get(pattern_id)
        if entry is None:
            return
        entry["verdict_pending"] = False
        save_state(state)


def verdict_pending_patterns() -> list[str]:
    """Return the ``pattern_id``s whose entry has a truthy ``verdict_pending``.

    These are the patterns whose surfaced hint awaits judgement on the
    user's next reply.
    """
    state = load_state()
    return [
        pattern_id
        for pattern_id, entry in state.items()
        if isinstance(entry, dict) and entry.get("verdict_pending")
    ]
