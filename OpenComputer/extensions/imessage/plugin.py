"""iMessage channel plugin — entry point.

Disabled by default. Mac-tied (BlueBubbles must run on a Mac). User
enables in profile + sets:

- ``BLUEBUBBLES_URL`` (e.g. ``http://localhost:1234``)
- ``BLUEBUBBLES_PASSWORD``

Optional: ``BLUEBUBBLES_POLL_INTERVAL`` (default 10s).
"""

from __future__ import annotations

import logging
import os

try:
    from adapter import IMessageAdapter  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.imessage.adapter import IMessageAdapter  # package mode

from plugin_sdk.core import Platform

logger = logging.getLogger("opencomputer.ext.imessage")


def register(api) -> None:  # PluginAPI duck-typed
    url = os.environ.get("BLUEBUBBLES_URL", "").strip()
    pw = os.environ.get("BLUEBUBBLES_PASSWORD", "").strip()
    if not url or not pw:
        logger.info(
            "imessage plugin: not registering (BLUEBUBBLES_URL / BLUEBUBBLES_PASSWORD unset)"
        )
        return
    interval = float(os.environ.get("BLUEBUBBLES_POLL_INTERVAL", "10"))
    adapter = IMessageAdapter(
        config={
            "base_url": url,
            "password": pw,
            "poll_interval_seconds": interval,
        }
    )
    api.register_channel(Platform.IMESSAGE.value, adapter)
    logger.info("imessage plugin: registered (BlueBubbles %s, poll=%ds)", url, interval)
