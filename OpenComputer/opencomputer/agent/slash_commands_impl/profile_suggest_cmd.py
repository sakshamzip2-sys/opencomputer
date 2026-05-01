"""``/profile-suggest`` — analyze persona usage and recommend profile actions.

User-pull only — runs the analysis in
:mod:`opencomputer.profile_analysis` against the current profile's
``sessions.db`` and renders a plain-text report. Suggests CREATE for
unmatched dominant personas and SWITCH for personas that match an
existing other profile.

No background work, no per-turn cost, no false-positive risk.
"""
from __future__ import annotations

import os
from pathlib import Path

from opencomputer.profile_analysis import (
    compute_profile_suggestions,
    render_report,
)
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


def _resolve_current_profile() -> str:
    """Resolve the active profile name.

    Order:
    1. ``OPENCOMPUTER_HOME`` env var pointing into ``profiles/<name>/``
       → returns ``<name>``.
    2. Sticky active-profile (from ``opencomputer.profiles``) if available.
    3. ``"default"`` fallback.
    """
    env_home = os.environ.get("OPENCOMPUTER_HOME")
    if env_home:
        p = Path(env_home).resolve()
        # Check if path matches ~/.opencomputer/profiles/<name>/
        if "profiles" in p.parts:
            try:
                idx = p.parts.index("profiles")
                if idx + 1 < len(p.parts):
                    return p.parts[idx + 1]
            except ValueError:
                pass
    try:
        from opencomputer.profiles import read_active_profile
        active = read_active_profile()
        if active:
            return active
    except Exception:  # noqa: BLE001
        pass
    return "default"


def _resolve_available_profiles() -> tuple[str, ...]:
    """List all profiles on disk under ``~/.opencomputer/profiles/*/``.

    Always includes ``"default"`` (the unnamed root profile).
    """
    out: list[str] = ["default"]
    try:
        from opencomputer.profiles import list_profiles
        out.extend(list_profiles())
    except Exception:  # noqa: BLE001
        pass
    # Dedupe while preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for name in out:
        if name not in seen:
            seen.add(name)
            uniq.append(name)
    return tuple(uniq)


class ProfileSuggestCommand(SlashCommand):
    name = "profile-suggest"
    description = (
        "Analyze recent persona usage and recommend profile create/switch actions"
    )

    async def execute(
        self, args: str, runtime: RuntimeContext,
    ) -> SlashCommandResult:
        db = runtime.custom.get("session_db")
        if db is None:
            return SlashCommandResult(
                output=(
                    "No active session DB — /profile-suggest only works "
                    "inside an agent loop turn."
                ),
                handled=True,
            )
        try:
            from opencomputer.agent.config import _home
            home = _home()
        except Exception:  # noqa: BLE001
            # _home() failed (e.g., ContextVar not initialized in test).
            # Fall back to the real ~/.opencomputer/, immune to $HOME
            # mutation by _apply_profile_override.
            from opencomputer.profiles import get_default_root
            home = get_default_root()

        current = _resolve_current_profile()
        available = _resolve_available_profiles()

        try:
            report = compute_profile_suggestions(
                home=home,
                db=db,
                current_profile=current,
                available_profiles=available,
            )
        except Exception as e:  # noqa: BLE001
            return SlashCommandResult(
                output=f"Profile analysis failed: {type(e).__name__}: {e}",
                handled=True,
            )
        return SlashCommandResult(output=render_report(report), handled=True)


__all__ = ["ProfileSuggestCommand"]
