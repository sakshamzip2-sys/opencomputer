"""
Telegram channel plugin — entry point.

Flat layout: plugin.py is the entry, sibling modules (adapter.py) are
importable via `from adapter import X` because the plugin loader puts
the plugin root on sys.path.
"""

from __future__ import annotations

import os

# The plugin loader adds this plugin's root dir to sys.path, so we can
# import sibling modules by their plain module name. This also works
# when pytest imports `extensions.telegram.adapter` directly.
try:
    from adapter import TelegramAdapter  # plugin-loader mode
except ImportError:  # pragma: no cover — happens when imported as a package
    from extensions.telegram.adapter import TelegramAdapter  # package mode

from plugin_sdk.core import Platform


def register(api) -> None:  # PluginAPI duck-typed
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    adapter = TelegramAdapter(config={"bot_token": token})
    api.register_channel(Platform.TELEGRAM.value, adapter)
