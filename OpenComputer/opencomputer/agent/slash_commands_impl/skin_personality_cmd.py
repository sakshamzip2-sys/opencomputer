"""``/skin [name]`` and ``/personality [name]`` — wired to real loaders.

- ``/personality`` (no args)        → show current + list available
- ``/personality NAME``             → set runtime + persist to config
- ``/personality reset|default``    → clear config (next session: helpful)

Same shape for ``/skin``. Both persist to the active profile's
``config.yaml`` so the choice survives across sessions.

The personality body is loaded from ``opencomputer.agent.personality``
(14 built-ins + custom from ``agent.personalities`` config) and
injected into the system prompt as slot #7 by ``PromptBuilder.build``.

The skin theme is loaded from ``opencomputer.cli_ui.skin`` (9 built-in
YAMLs + custom from ``~/.opencomputer/skins/<name>.yaml``) and applied
to the live Rich Console at session start. Mid-session swap updates
spinner verbs and branding immediately; the color theme requires a
session restart since the slash dispatcher does not pass the live
Console handle.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from opencomputer.agent.personality import BUILTINS as _PERS_BUILTINS
from opencomputer.agent.profile_yaml import (
    get_custom_personalities,
    set_default_personality,
    set_display_skin,
)
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

logger = logging.getLogger(__name__)

_RESET_TOKENS = frozenset({"reset", "default", "off", "clear"})


def _profile_config_path() -> Path:
    home = os.environ.get(
        "OPENCOMPUTER_HOME",
        str(Path.home() / ".opencomputer"),
    )
    profile = os.environ.get("OPENCOMPUTER_PROFILE", "default")
    return Path(home) / profile / "config.yaml"


def _builtin_skin_names() -> list[str]:
    """Lazy import — avoid CLI startup cost when skins not used."""
    try:
        from opencomputer.cli_ui.skin import list_builtin_names
        return list_builtin_names()
    except Exception:  # noqa: BLE001 — never crash on listing
        return ["default"]


class PersonalityCommand(SlashCommand):
    name = "personality"
    description = "Get or set the active personality (prompt overlay)"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = runtime.custom.get("personality") or "helpful"
        available = sorted(_PERS_BUILTINS.keys())

        if sub == "":
            return SlashCommandResult(
                output=(
                    f"Current personality: {current}\n"
                    f"Available built-in: {', '.join(available)}\n"
                    f"(custom personalities go under "
                    f"`agent.personalities` in config.yaml)"
                ),
                handled=True,
            )

        if sub in _RESET_TOKENS:
            runtime.custom["personality"] = "helpful"
            try:
                set_default_personality(_profile_config_path(), "")
            except OSError as exc:
                return SlashCommandResult(
                    output=f"Reset runtime, but config write failed: {exc}",
                    handled=True,
                )
            return SlashCommandResult(
                output="Personality reset to default (helpful).",
                handled=True,
            )

        # Validate against built-ins + any custom personalities from
        # config. Custom names declared in agent.personalities are
        # accepted live (no restart needed).
        custom_names = set(get_custom_personalities(_profile_config_path()))
        if sub not in _PERS_BUILTINS and sub not in custom_names:
            extra = (
                f" Custom: {', '.join(sorted(custom_names))}.\n"
                if custom_names else ""
            )
            return SlashCommandResult(
                output=(
                    f"Unknown personality {sub!r}. "
                    f"Built-in: {', '.join(available)}.\n{extra}"
                    f"(define more under `agent.personalities` in config.yaml)"
                ),
                handled=True,
            )

        runtime.custom["personality"] = sub
        try:
            set_default_personality(_profile_config_path(), sub)
        except OSError as exc:
            return SlashCommandResult(
                output=(
                    f"Personality set to {sub} (runtime only — "
                    f"config write failed: {exc})"
                ),
                handled=True,
            )
        return SlashCommandResult(
            output=f"Personality set to {sub} (persisted to config).",
            handled=True,
        )


class SkinCommand(SlashCommand):
    name = "skin"
    description = "Get or set the active TUI skin"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = runtime.custom.get("skin") or "default"
        available = _builtin_skin_names()

        if sub == "":
            return SlashCommandResult(
                output=(
                    f"Current skin: {current}\n"
                    f"Built-in: {', '.join(available)}\n"
                    f"(drop custom YAML at "
                    f"~/.opencomputer/skins/<name>.yaml)"
                ),
                handled=True,
            )

        if sub in _RESET_TOKENS:
            runtime.custom["skin"] = "default"
            try:
                set_display_skin(_profile_config_path(), "")
            except OSError as exc:
                return SlashCommandResult(
                    output=f"Reset runtime, but config write failed: {exc}",
                    handled=True,
                )
            _apply_skin_with_live_console(runtime, "default")
            return SlashCommandResult(
                output="Skin reset to default (live repaint applied).",
                handled=True,
            )

        # Validate by attempting to load the skin and checking the
        # resolved name. load_skin falls back to "default" with a
        # warning if neither built-in nor user file exists; we use
        # that to detect typos and refuse here.
        try:
            from opencomputer.cli_ui.skin import load_skin
            spec = load_skin(sub)
            if spec.name != sub:
                return SlashCommandResult(
                    output=(
                        f"Unknown skin {sub!r}. "
                        f"Built-in: {', '.join(available)}\n"
                        f"(drop custom YAML at "
                        f"~/.opencomputer/skins/{sub}.yaml)"
                    ),
                    handled=True,
                )
        except Exception as exc:  # noqa: BLE001 — never crash on validation
            logger.warning("skin: validation failed for %r — %s", sub, exc)

        runtime.custom["skin"] = sub
        # Hermes v2 D7 (2026-05-08): full live repaint.
        # When the CLI input loop puts its live Rich Console under
        # ``runtime.custom["live_console"]`` (or
        # ``"_live_console"``), the slash command pushes the theme onto
        # that live console — so spinner / branding / colors all
        # hot-swap without a session restart. Channel adapters and the
        # gateway don't have a live console, so they get spinner +
        # branding only (color theme is a no-op there anyway).
        live = _apply_skin_with_live_console(runtime, sub)

        try:
            set_display_skin(_profile_config_path(), sub)
        except OSError as exc:
            return SlashCommandResult(
                output=(
                    f"Skin set to {sub} (runtime only — "
                    f"config write failed: {exc})"
                ),
                handled=True,
            )
        if live:
            tail = "color theme + spinner + branding applied live."
        else:
            tail = (
                "spinner + branding applied; color theme will fully "
                "repaint on next refresh."
            )
        return SlashCommandResult(
            output=f"Skin set to {sub} (persisted to config). {tail}",
            handled=True,
        )


def _try_apply_skin_to_module_state(name: str) -> None:
    """Best-effort skin apply for module-global state (no live console).

    Uses a throwaway Console for the theme push (which won't affect any
    live console) but DOES update the module-global spinner/branding/
    tool_emoji state that renderers consult on next render.

    Used as a fallback when no live console is available (channel
    adapters, gateway).
    """
    try:
        from rich.console import Console

        from opencomputer.cli_ui.skin import apply_skin, load_skin
        spec = load_skin(name)
        apply_skin(spec, Console())
    except Exception as exc:  # noqa: BLE001 — never crash on hot-swap
        logger.warning("skin: hot-swap failed for %r — %s", name, exc)


def _apply_skin_with_live_console(
    runtime: RuntimeContext, name: str
) -> bool:
    """Apply the skin to the live console if the CLI provided one.

    Hermes v2 D7 (2026-05-08): if the caller (typically the CLI input
    loop) has set ``runtime.custom["live_console"]`` or
    ``"_live_console"`` to a live Rich Console, push the theme onto it
    so the color repaint takes effect immediately. Otherwise fall back
    to the throwaway-console path that still updates module-global
    spinner / branding / tool-emoji state.

    Returns ``True`` when a live console was used (full repaint), or
    ``False`` when only module-state was updated.
    """
    live = runtime.custom.get("live_console") or runtime.custom.get(
        "_live_console"
    )
    if live is None:
        _try_apply_skin_to_module_state(name)
        return False
    try:
        from opencomputer.cli_ui.skin import apply_skin, load_skin
        spec = load_skin(name)
        apply_skin(spec, live)
        return True
    except Exception as exc:  # noqa: BLE001 — never crash on hot-swap
        logger.warning("skin: live hot-swap failed for %r — %s", name, exc)
        # Best-effort fallback so module state still updates.
        _try_apply_skin_to_module_state(name)
        return False


__all__ = ["PersonalityCommand", "SkinCommand"]
