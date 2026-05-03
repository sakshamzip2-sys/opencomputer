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

    # P2-13: subscribe the admin to policy-engine notifications when
    # an admin chat id is configured. Without it, the engine still runs
    # — the user can fall back to /policy-changes for review — but no
    # ambient pings land. Sub-project A made Honcho memory the always-on
    # default; this is the corresponding always-on UX for policy decisions
    # when Telegram is the active surface.
    admin_chat = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
    if admin_chat:
        try:
            from opencomputer.ingestion.bus import get_default_bus

            try:
                from policy_notifier import (  # plugin-loader mode
                    register_policy_notifier,
                    register_revert_notifier,
                )
            except ImportError:  # pragma: no cover — package mode
                from extensions.telegram.policy_notifier import (
                    register_policy_notifier,
                    register_revert_notifier,
                )

            bus = get_default_bus()
            if bus is not None:
                register_policy_notifier(
                    bus=bus,
                    admin_chat_id=admin_chat,
                    send_fn=adapter.send,
                )
                register_revert_notifier(
                    bus=bus,
                    admin_chat_id=admin_chat,
                    send_fn=adapter.send,
                )
        except Exception:  # noqa: BLE001 — wiring failure must not break adapter
            pass
