"""
Telegram channel plugin — entry point.

OpenComputer calls `register(api)` at plugin activation.
"""

from __future__ import annotations

import os

from extensions.telegram.src.adapter import TelegramAdapter
from plugin_sdk.core import Platform


def register(api) -> None:  # PluginAPI duck-typed
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        # No token configured — still register the channel class,
        # but it won't connect. User can set TELEGRAM_BOT_TOKEN later.
        return
    adapter = TelegramAdapter(config={"bot_token": token})
    api.register_channel(Platform.TELEGRAM.value, adapter)
