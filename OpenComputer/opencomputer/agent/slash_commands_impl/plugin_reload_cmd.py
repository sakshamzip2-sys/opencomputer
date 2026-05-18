"""``/plugin reload <plugin-id>`` — hot-reload a single plugin
in-place without restarting the chat session.

Backs the user-facing UX promise from pi:

    The agent writes an extension, pi reloads it, the agent uses it.

For OC, plugins are Python and reload goes through
:func:`opencomputer.plugins.loader.reload_plugin`. The reload tears
down the existing registrations + clears the plugin's synthetic
``sys.modules`` entry, then re-imports the entry module from disk
and re-registers.

Failure modes (each surfaced as a distinct user-readable message):

* No plugin id passed — show usage hint.
* Plugin id not currently loaded — list candidates so the user can
  retry with the right id.
* PluginRegistry not threaded through ``runtime.custom`` — return a
  hard error rather than fail silently.
* load_plugin raises during the re-import — message bubbles up with
  the exception class + summary.

The reloaded plugin's new tool / hook / slash counts are surfaced in
the success message so the user can verify the registrations stuck.
"""

from __future__ import annotations

import logging
from typing import Any

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_log = logging.getLogger(__name__)


class PluginReloadCommand(SlashCommand):
    name = "plugin"
    aliases = ("plug",)
    description = "Hot-reload a plugin in place — usage: /plugin reload <id>"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        parts = (args or "").strip().split(maxsplit=1)
        if not parts or parts[0] not in {"reload"}:
            return SlashCommandResult(
                output=(
                    "usage: /plugin reload <plugin-id|all>\n"
                    "  reload — re-import a plugin's entry module and "
                    "re-register tools / hooks / slash commands\n"
                    "  use 'all' to reload every loaded plugin at once"
                ),
                handled=True,
            )
        if len(parts) < 2 or not parts[1].strip():
            return SlashCommandResult(
                output="usage: /plugin reload <plugin-id> (id is required)",
                handled=True,
            )
        target_id = parts[1].strip()

        registry = runtime.custom.get("plugin_registry")
        if registry is None:
            return SlashCommandResult(
                output=(
                    "error: PluginRegistry is not wired into runtime — "
                    "this build can't hot-reload plugins. Restart the "
                    "session instead."
                ),
                handled=True,
            )

        # Run the reload via the loader helper — single source of truth
        # for the teardown + re-import sequence.
        from opencomputer.plugins.loader import reload_plugin

        api = registry.shared_api
        if api is None:
            return SlashCommandResult(
                output=(
                    "error: PluginRegistry has no shared_api — "
                    "load_all() has not run on this registry. Cannot "
                    "reload."
                ),
                handled=True,
            )

        def _reload_one(loaded: Any) -> tuple[bool, str]:
            """Reload one LoadedPlugin, swapping it into registry.loaded
            in place on success. Returns ``(ok, message)``."""
            new_loaded, message = reload_plugin(loaded, api)
            if new_loaded is None:
                return (False, message)
            # Keep ordering stable so registry.loaded iteration order
            # doesn't shuffle around the user.
            for i, lp in enumerate(registry.loaded):
                if lp is loaded:
                    registry.loaded[i] = new_loaded
                    break
            return (True, message)

        # ── /plugin reload all — reload every loaded plugin ──────────
        # The plugin-author dev loop: edit several plugins, reload all.
        if target_id == "all":
            # Snapshot first — _reload_one mutates registry.loaded.
            targets = list(registry.loaded)
            if not targets:
                return SlashCommandResult(
                    output="no plugins are currently loaded — nothing to reload.",
                    handled=True,
                )
            ok_lines: list[str] = []
            fail_lines: list[str] = []
            for loaded in targets:
                pid = loaded.candidate.manifest.id
                ok, message = _reload_one(loaded)
                if ok:
                    ok_lines.append(f"  ✓ {message}")
                else:
                    _log.warning(
                        "/plugin reload all: %s failed: %s", pid, message
                    )
                    fail_lines.append(f"  ✗ {pid}: {message}")
            summary = (
                f"reloaded {len(ok_lines)}/{len(targets)} plugins"
                + (f", {len(fail_lines)} failed" if fail_lines else "")
            )
            return SlashCommandResult(
                output="\n".join([summary, *ok_lines, *fail_lines]),
                handled=True,
            )

        # ── /plugin reload <id> — single plugin ──────────────────────
        loaded = next(
            (
                lp
                for lp in registry.loaded
                if lp.candidate.manifest.id == target_id
            ),
            None,
        )
        if loaded is None:
            avail = sorted(
                lp.candidate.manifest.id for lp in registry.loaded
            )
            avail_hint = ", ".join(avail) if avail else "(none loaded)"
            return SlashCommandResult(
                output=(
                    f"error: no plugin {target_id!r} currently loaded.\n"
                    f"loaded plugins: {avail_hint}"
                ),
                handled=True,
            )

        ok, message = _reload_one(loaded)
        if not ok:
            _log.warning("/plugin reload %s failed: %s", target_id, message)
            return SlashCommandResult(
                output=f"reload failed for {target_id!r}: {message}\n"
                       f"the plugin is now UNLOADED — its tools / hooks "
                       f"are no longer registered.",
                handled=True,
            )
        _log.info("/plugin reload succeeded: %s", message)
        return SlashCommandResult(output=f"✓ {message}", handled=True)


__all__ = ["PluginReloadCommand"]
