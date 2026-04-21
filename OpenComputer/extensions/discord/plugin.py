"""Discord channel plugin — entry point."""

from __future__ import annotations

import os

try:
    from adapter import DiscordAdapter
except ImportError:  # pragma: no cover
    from extensions.discord.adapter import DiscordAdapter

from plugin_sdk.core import Platform


def register(api) -> None:  # PluginAPI duck-typed
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return
    adapter = DiscordAdapter(config={"bot_token": token})
    api.register_channel(Platform.DISCORD.value, adapter)
