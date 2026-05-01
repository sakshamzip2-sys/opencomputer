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
        # Plan 3 (2026-05-01) — accept/dismiss subcommands.
        parts = args.strip().split(maxsplit=1)
        sub = parts[0] if parts else ""
        target = parts[1] if len(parts) > 1 else ""

        if sub == "accept":
            return await self._accept(target, runtime)
        if sub == "dismiss":
            return await self._dismiss(target, runtime)

        # Existing analysis path (unchanged).
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
            home = Path.home() / ".opencomputer"

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

    async def _accept(
        self, name: str, runtime: RuntimeContext,
    ) -> SlashCommandResult:
        """Create the suggested profile + write seeded SOUL.md."""
        if not name:
            return SlashCommandResult(
                output="Usage: /profile-suggest accept <name>",
                handled=True,
            )

        from opencomputer.profile_analysis_daily import (
            DailySuggestion,
            load_cache,
            save_cache,
        )
        from opencomputer.profile_seeder import render_seeded_soul
        from opencomputer.profiles import (
            ProfileExistsError,
            create_profile,
            get_profile_dir,
        )

        cache = load_cache()
        if not cache:
            return SlashCommandResult(
                output=(
                    "No suggestion cache found. Run "
                    "`oc profile analyze run` first."
                ),
                handled=True,
            )

        suggestion_data = next(
            (s for s in cache.get("suggestions", []) if s.get("name") == name),
            None,
        )
        if not suggestion_data:
            return SlashCommandResult(
                output=(
                    f"No pending suggestion for '{name}'. Run "
                    "`/profile-suggest` to see current suggestions."
                ),
                handled=True,
            )

        suggestion = DailySuggestion(**suggestion_data)
        user_name = runtime.custom.get("user_name", "the user")

        try:
            create_profile(name)
        except ProfileExistsError:
            return SlashCommandResult(
                output=f"Profile '{name}' already exists.",
                handled=True,
            )

        profile_dir = get_profile_dir(name)
        soul_path = profile_dir / "SOUL.md"
        soul_path.parent.mkdir(parents=True, exist_ok=True)
        soul_path.write_text(
            render_seeded_soul(suggestion, user_name=user_name)
        )

        # Remove the accepted suggestion from the cache.
        remaining = [
            s for s in cache.get("suggestions", []) if s.get("name") != name
        ]
        save_cache(
            suggestions=[DailySuggestion(**s) for s in remaining],
            dismissed=cache.get("dismissed", []),
        )

        return SlashCommandResult(
            output=(
                f"✅ Profile '{name}' created with seeded SOUL.md.\n"
                f"   Switch to it: Ctrl+P  (or restart with `oc -p {name}`)"
            ),
            handled=True,
        )

    async def _dismiss(
        self, name: str, runtime: RuntimeContext,
    ) -> SlashCommandResult:
        """Mark a suggestion as dismissed for 7 days."""
        if not name:
            return SlashCommandResult(
                output="Usage: /profile-suggest dismiss <name>",
                handled=True,
            )
        from opencomputer.profile_analysis_daily import record_dismissal
        record_dismissal(name)
        return SlashCommandResult(
            output=f"Suggestion '{name}' dismissed for 7 days.",
            handled=True,
        )


__all__ = ["ProfileSuggestCommand"]
