"""Profile-cycling and pending-swap helpers (Plan 1 of 3).

Kept out of ``input_loop.py`` so we can unit-test without the full
prompt_toolkit ``Application``. The Ctrl+P key binding in
``input_loop.py`` calls :func:`cycle_profile`; the turn-entry hook in
``agent/loop.py`` calls :func:`consume_pending_profile_swap`.

Backwards compat note: persona state in ``runtime.custom`` is left
strictly alone here. Plan 2 (persona-removal) will retire those keys.
"""
from __future__ import annotations

from typing import Any

_NO_OTHER_PROFILES_HINT = "no other profiles — use /profile create"


def _all_cycle_targets() -> list[str]:
    """Sorted list of cycle targets, ALWAYS including ``"default"``.

    Per user request, the cycle now rotates through ``default`` plus
    every real profile on disk (e.g. ``default → coding → stock →
    default``). Previously ``default`` was hidden once real profiles
    existed, leaving Ctrl+P "stuck" rotating between non-default
    profiles only — the user could never get back to the no-sticky
    state without dropping to the CLI.

    Empty list edge case: zero real profiles → just ``["default"]``,
    which trips the ``len(targets) <= 1`` guard in
    :func:`cycle_profile` and surfaces the "no other profiles" hint.
    """
    from opencomputer.profiles import list_profiles

    names = sorted(list_profiles())
    # Guarantee "default" is in the cycle and lives at the front so
    # the rotation reads naturally: default → first-real-profile → … → default.
    if "default" not in names:
        names = ["default", *names]
    return names


def cycle_profile(runtime: Any) -> str | None:
    """Advance the runtime's pending profile to the next available.

    Mutates ``runtime.custom["pending_profile_id"]``. Returns the new
    pending id, or ``None`` if there's only one profile (default-only).
    Sets ``runtime.custom["profile_cycle_hint"]`` for one render-tick
    when there's nothing to cycle to.
    """
    targets = _all_cycle_targets()
    if len(targets) <= 1:
        runtime.custom["profile_cycle_hint"] = _NO_OTHER_PROFILES_HINT
        return None

    current = (
        runtime.custom.get("pending_profile_id")
        or runtime.custom.get("active_profile_id")
        or "default"
    )
    try:
        idx = targets.index(current)
    except ValueError:
        idx = -1
    new_id = targets[(idx + 1) % len(targets)]
    runtime.custom["pending_profile_id"] = new_id
    runtime.custom.pop("profile_cycle_hint", None)
    return new_id


def consume_pending_profile_swap(runtime: Any) -> str | None:
    """Apply ``pending_profile_id`` if set. Called at turn entry.

    Pure: only mutates ``runtime.custom`` and writes the sticky
    ``active_profile`` file. Memory rebinding and prompt-cache eviction
    are the caller's responsibility (handled in ``agent/loop.py``).

    Returns the new active profile id, or ``None`` if no swap occurred.
    """
    pending = runtime.custom.pop("pending_profile_id", None)
    if not pending:
        return None
    current = runtime.custom.get("active_profile_id") or "default"
    if pending == current:
        return None

    from opencomputer.profiles import write_active_profile
    write_active_profile(None if pending == "default" else pending)
    runtime.custom["active_profile_id"] = pending
    return pending


def init_active_profile_id(runtime: Any) -> None:
    """Mirror the sticky ``active_profile`` file into runtime.custom on
    first turn of a session. Idempotent — runs only when the key is
    missing.
    """
    if "active_profile_id" in runtime.custom:
        return
    from opencomputer.profiles import read_active_profile
    runtime.custom["active_profile_id"] = read_active_profile() or "default"
