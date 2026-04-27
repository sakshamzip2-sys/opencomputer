"""Home Assistant channel plugin — entry point.

Outbound only via REST API. Inbound: webhook adapter (G.3) wired to a
Home Assistant automation that POSTs events.

Env vars: ``HOMEASSISTANT_URL`` (e.g. ``http://homeassistant.local:8123``)
and ``HOMEASSISTANT_TOKEN`` (long-lived access token from your HA
profile page). Disabled by default.
"""

from __future__ import annotations

import logging
import os

try:
    from adapter import HomeAssistantAdapter  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.homeassistant.adapter import (  # package mode
        HomeAssistantAdapter,
    )

from plugin_sdk.core import Platform

logger = logging.getLogger("opencomputer.ext.homeassistant")


def register(api) -> None:  # PluginAPI duck-typed
    url = os.environ.get("HOMEASSISTANT_URL", "").strip()
    token = os.environ.get("HOMEASSISTANT_TOKEN", "").strip()
    if not url or not token:
        logger.info(
            "homeassistant plugin: not registering "
            "(HOMEASSISTANT_URL or HOMEASSISTANT_TOKEN unset)"
        )
        return
    adapter = HomeAssistantAdapter(config={"url": url, "token": token})
    api.register_channel(Platform.HOMEASSISTANT.value, adapter)
    logger.info("homeassistant plugin: registered for %s", url)
