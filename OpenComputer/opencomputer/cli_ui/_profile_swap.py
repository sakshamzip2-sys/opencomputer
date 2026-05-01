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
    """Sorted list of cycle targets including the implicit ``default``.

    ``list_profiles()`` only returns subdirs of ``~/.opencomputer/profiles/``;
    "default" is the implicit fallback when no active_profile file is set.
    The cycler treats "default" as a first-class entry so the user can
    always cycle back to it.
    """
    from opencomputer.profiles import list_profiles

    names = list_profiles()
    if "default" not in names:
        names = sorted([*names, "default"])
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
    """Consume a pending profile swap and return the new profile id.

    Implemented in Task 2.
    """
    raise NotImplementedError("Implemented in Task 2")
