"""Discord channel plugin — entry point.

Two registrations:

* The gateway adapter (DM + channel ingress) — only when
  ``DISCORD_BOT_TOKEN`` is present (the adapter actively connects).
* The ``discord_server`` introspection / management tool — registered
  unconditionally (Hermes parity, 2026-05-01). The tool inspects the
  token at call time and returns a structured error when it's missing,
  so the model still sees a useful schema in environments where the
  bot isn't yet provisioned.
"""

from __future__ import annotations

import logging
import os

try:
    from adapter import DiscordAdapter
except ImportError:  # pragma: no cover
    from extensions.discord.adapter import DiscordAdapter

from plugin_sdk.core import Platform

_log = logging.getLogger("opencomputer.discord.plugin")


def register(api) -> None:  # PluginAPI duck-typed
    # Tool registration is unconditional — the tool's execute() path
    # handles missing-token. This matches Hermes' behaviour where the
    # schema is always exposed and runtime errors guide the user to
    # configure the token if they actually try to call it.
    try:
        try:
            from server_tool import DiscordServerTool
        except ImportError:  # pragma: no cover
            from extensions.discord.server_tool import DiscordServerTool
        api.register_tool(DiscordServerTool())
    except Exception as exc:  # noqa: BLE001
        _log.warning("DiscordServerTool registration failed: %s", exc)

    # Channel adapter requires the token to actually connect.
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return
    adapter = DiscordAdapter(config={"bot_token": token})
    api.register_channel(Platform.DISCORD.value, adapter)
