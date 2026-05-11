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
                    "usage: /plugin reload <plugin-id>\n"
                    "  reload — re-import a plugin's entry module and "
                    "re-register tools / hooks / slash commands"
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

        new_loaded, message = reload_plugin(loaded, api)
        if new_loaded is None:
            _log.warning("/plugin reload %s failed: %s", target_id, message)
            return SlashCommandResult(
                output=f"reload failed for {target_id!r}: {message}\n"
                       f"the plugin is now UNLOADED — its tools / hooks "
                       f"are no longer registered.",
                handled=True,
            )

        # Replace the stale LoadedPlugin entry in registry.loaded with
        # the freshly-loaded one. Keep ordering stable so iteration
        # order of registry.loaded doesn't shuffle around the user.
        for i, lp in enumerate(registry.loaded):
            if lp is loaded:
                registry.loaded[i] = new_loaded
                break
        _log.info("/plugin reload succeeded: %s", message)
        return SlashCommandResult(
            output=f"✓ {message}",
            handled=True,
        )


__all__ = ["PluginReloadCommand"]
