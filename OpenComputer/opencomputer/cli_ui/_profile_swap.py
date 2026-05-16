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

    Mutates **three** sources of truth in lockstep so every code path
    that resolves "what is the active profile" sees the new value:

    1. ``runtime.custom["active_profile_id"]`` (this process, agent loop)
    2. ``~/.opencomputer/active_profile`` sticky file (filesystem, all
       processes — read by :func:`scope_subprocess_env`)
    3. ``os.environ["OPENCOMPUTER_HOME"]`` (this process — read by
       :func:`_home`, dotenv, plugin path resolution)

    Plus best-effort reset of the
    ``plugin_sdk.profile_context.current_profile_home`` ContextVar so
    plugins that pinned it to the old profile see the new one on next
    lazy resolution.

    Closes the §3 split-brain documented in
    ``docs/plans/profile-handoff-investigation.md``: before this fix,
    only (1) + (2) updated, leaving in-process callers of ``_home()``
    pinned to the old profile while subprocesses saw the new one.

    Memory rebinding and prompt-cache eviction remain the caller's
    responsibility (handled in ``agent/loop.py``); this helper only
    owns the three-state environment switch.

    Returns the new active profile id, or ``None`` if no swap occurred.
    """
    pending = runtime.custom.pop("pending_profile_id", None)
    if not pending:
        return None
    current = runtime.custom.get("active_profile_id") or "default"
    if pending == current:
        return None

    import os

    from opencomputer.profiles import get_profile_dir, write_active_profile

    # (2) sticky file
    write_active_profile(None if pending == "default" else pending)

    # (3) env var — this closes §3 split-brain. ``get_profile_dir(None)``
    # returns the default root (~/.opencomputer/ or test-override via
    # OPENCOMPUTER_HOME_ROOT); ``get_profile_dir(name)`` returns
    # ~/.opencomputer/profiles/<name>/. Either way it's the canonical
    # path that _home() should resolve to going forward. Subprocesses
    # spawned BEFORE this point inherited the old value at fork-time
    # and keep it (correct); subprocesses spawned AFTER inherit the new
    # value (correct). The mutation is therefore safe — no retroactive
    # surprise for in-flight children.
    new_home = get_profile_dir(None if pending == "default" else pending)
    os.environ["OPENCOMPUTER_HOME"] = str(new_home)

    # Reset the plugin-sdk ContextVar so any plugin task that pinned it
    # to the old profile cedes back to the env-var-resolved default on
    # next read. We do NOT set it to the new home — that would mask
    # the plugin's per-task scoping intent. Resetting to None means
    # plugin_sdk re-reads the env var (which we just updated).
    try:
        from plugin_sdk.profile_context import current_profile_home

        current_profile_home.set(None)
    except Exception:  # noqa: BLE001 — plugin_sdk shape change shouldn't wedge swap
        pass

    # (1) runtime.custom
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
