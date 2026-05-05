"""On-disk state + shared helpers for the social-traces plugin.

Mirrors the skill-evolution plugin's state model (see
``extensions/skill-evolution/subscriber.py``):

* ``<profile_home>/traces/state.json`` — ``{"enabled": <bool>}``. Missing
  file = disabled (the plugin ships opt-in).
* ``<profile_home>/traces/heartbeat`` — timestamp written each time the
  subscriber observes an event while enabled. Lets the operator confirm
  the wiring without enabling the LLM pipeline.

State is read by the BEFORE_TASK hook and the post-task subscriber on
every fire. CLI verbs (``oc traces enable/disable/status``) read + write
through here.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

_log = logging.getLogger("opencomputer.social_traces.state")


# ─── profile-home resolver ──────────────────────────────────────────
#
# We avoid importing ``opencomputer.agent.config._home()`` here so the
# extension stays inside the plugin_sdk + stdlib boundary. Resolution
# order mirrors ``_home``:
#   1. OPENCOMPUTER_PROFILE_HOME env (test override)
#   2. plugin_sdk.current_profile_home ContextVar (set by gateway
#      dispatch in production)
#   3. OPENCOMPUTER_HOME env (legacy single-profile path)
#   4. ~/.opencomputer (final fallback)


def resolve_profile_home() -> Path:
    """Return the active profile's home dir without importing
    from ``opencomputer.*``.

    Production callers invoke this through ``plugin.py``'s
    ``_profile_home_factory``; tests pass an explicit path via
    ``runtime.custom['profile_home']`` and never hit this resolver.
    """
    env = os.environ.get("OPENCOMPUTER_PROFILE_HOME")
    if env:
        return Path(env)

    try:
        from plugin_sdk.profile_context import current_profile_home

        ctx_val = current_profile_home.get()
        if ctx_val is not None:
            return ctx_val
    except Exception:  # noqa: BLE001 — defensive for SDK-version mismatch
        pass

    env = os.environ.get("OPENCOMPUTER_HOME")
    if env:
        return Path(env)

    return Path.home() / ".opencomputer"

STATE_FILENAME = "state.json"
HEARTBEAT_FILENAME = "heartbeat"
TRACES_DIRNAME = "traces"


def traces_dir(profile_home: Path) -> Path:
    """Return ``<profile_home>/traces/`` (does not create it)."""
    return profile_home / TRACES_DIRNAME


def state_path(profile_home: Path) -> Path:
    return traces_dir(profile_home) / STATE_FILENAME


def heartbeat_path(profile_home: Path) -> Path:
    return traces_dir(profile_home) / HEARTBEAT_FILENAME


def read_state(profile_home: Path) -> dict:
    """Read the JSON state file. Returns empty dict on missing/malformed."""
    try:
        raw = state_path(profile_home).read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _log.warning(
            "social-traces: malformed state.json at %s — treating as disabled",
            state_path(profile_home),
        )
        return {}


def write_state(profile_home: Path, state: dict) -> None:
    """Atomically write the state file (parent dir created on demand)."""
    path = state_path(profile_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def is_enabled(profile_home: Path) -> bool:
    """Convenience: ``True`` iff the on-disk flag is set."""
    return bool(read_state(profile_home).get("enabled", False))


def set_enabled(profile_home: Path, enabled: bool) -> None:
    """Flip the enabled flag. Preserves any other keys in the state file."""
    state = read_state(profile_home)
    state["enabled"] = bool(enabled)
    write_state(profile_home, state)


def write_heartbeat(profile_home: Path) -> None:
    """Best-effort heartbeat write. Failures log at DEBUG only — never raise."""
    path = heartbeat_path(profile_home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()))
    except OSError:
        _log.debug("social-traces: heartbeat write failed", exc_info=True)


def read_heartbeat(profile_home: Path) -> float:
    """Return the float timestamp from the heartbeat file, or ``0.0``."""
    try:
        return float(heartbeat_path(profile_home).read_text(encoding="utf-8").strip())
    except (OSError, FileNotFoundError, ValueError):
        return 0.0


__all__ = [
    "HEARTBEAT_FILENAME",
    "STATE_FILENAME",
    "TRACES_DIRNAME",
    "heartbeat_path",
    "is_enabled",
    "read_heartbeat",
    "read_state",
    "resolve_profile_home",
    "set_enabled",
    "state_path",
    "traces_dir",
    "write_heartbeat",
    "write_state",
]
