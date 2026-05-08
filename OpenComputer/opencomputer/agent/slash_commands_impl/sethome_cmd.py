"""``/sethome <platform> <chat_id>`` — set / list / clear home channels.

Mirrors the ``oc gateway sethome`` Typer command (see
:func:`opencomputer.cli_gateway.cmd_sethome`) so the same routing
metadata is reachable from the in-chat slash dispatch path used by
non-CLI surfaces (gateway / wire / ACP).

Usage:
    /sethome telegram 12345               → set home for telegram
    /sethome telegram 12345 thread:42     → optional thread suffix
    /sethome --list                       → list all entries
    /sethome --clear telegram             → drop entry for telegram

The mapping is persisted to ``<profile_home>/gateway/home_channels.json``
in the same JSON shape the CLI version writes — both surfaces remain
interchangeable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class SethomeCommand(SlashCommand):
    name = "sethome"
    description = "Set, list, or clear messaging-gateway home channels"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        tokens = (args or "").split()
        home_path = _home_channels_path(runtime)
        home_path.parent.mkdir(parents=True, exist_ok=True)
        mapping = _load_mapping(home_path)

        # /sethome --list
        if tokens and tokens[0] == "--list":
            if not mapping:
                return SlashCommandResult(
                    output="no home channels set", handled=True,
                )
            lines = [f"{plat}: {val}" for plat, val in mapping.items()]
            return SlashCommandResult(
                output="\n".join(lines), handled=True,
            )

        # /sethome --clear <platform>
        if tokens and tokens[0] == "--clear":
            if len(tokens) < 2:
                return SlashCommandResult(
                    output="usage: /sethome --clear <platform>", handled=True,
                )
            plat = tokens[1]
            if plat in mapping:
                del mapping[plat]
                home_path.write_text(
                    json.dumps(mapping, indent=2), encoding="utf-8",
                )
                return SlashCommandResult(
                    output=f"cleared home for {plat}", handled=True,
                )
            return SlashCommandResult(
                output=f"no home set for {plat}", handled=True,
            )

        # /sethome <platform> <chat_id> [thread]
        if len(tokens) < 2:
            return SlashCommandResult(
                output=(
                    "usage: /sethome <platform> <chat_id> [thread]\n"
                    "       /sethome --list\n"
                    "       /sethome --clear <platform>"
                ),
                handled=True,
            )
        platform = tokens[0]
        chat_id = tokens[1]
        thread = tokens[2] if len(tokens) >= 3 else None
        val = chat_id if not thread else f"{chat_id}:{thread}"
        mapping[platform] = val
        home_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
        return SlashCommandResult(
            output=f"home set: {platform} -> {val}", handled=True,
        )


def _home_channels_path(runtime: RuntimeContext) -> Path:
    """Resolve ``<profile_home>/gateway/home_channels.json`` from runtime.

    Resolution order:
      1. ``runtime.custom['profile_home']`` if set (preferred — set by
         the agent loop / gateway dispatch before slash routing).
      2. ``OPENCOMPUTER_HOME`` env var (matches the CLI fallback).
      3. ``~/.opencomputer/<OPENCOMPUTER_PROFILE or 'default'>``.
    """
    custom = runtime.custom or {}
    home = custom.get("profile_home")
    if home is not None:
        return Path(home) / "gateway" / "home_channels.json"

    env_home = os.environ.get("OPENCOMPUTER_HOME")
    profile = os.environ.get("OPENCOMPUTER_PROFILE", "default")
    if env_home:
        return Path(env_home) / profile / "gateway" / "home_channels.json"
    return (
        Path.home() / ".opencomputer" / profile / "gateway" / "home_channels.json"
    )


def _load_mapping(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


__all__ = ["SethomeCommand"]
