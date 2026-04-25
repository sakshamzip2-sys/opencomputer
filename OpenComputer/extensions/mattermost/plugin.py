"""Mattermost channel plugin — entry point.

Outbound only via Web API. Inbound: use Mattermost Outgoing Webhooks →
OC webhook adapter (G.3). Disabled by default.

Env vars: ``MATTERMOST_URL`` and ``MATTERMOST_TOKEN`` (Personal Access Token).
"""

from __future__ import annotations

import logging
import os

try:
    from adapter import MattermostAdapter  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.mattermost.adapter import MattermostAdapter  # package mode

from plugin_sdk.core import Platform

logger = logging.getLogger("opencomputer.ext.mattermost")


def register(api) -> None:  # PluginAPI duck-typed
    url = os.environ.get("MATTERMOST_URL", "").strip()
    token = os.environ.get("MATTERMOST_TOKEN", "").strip()
    if not url or not token:
        logger.info(
            "mattermost plugin: not registering (MATTERMOST_URL or MATTERMOST_TOKEN unset)"
        )
        return
    adapter = MattermostAdapter(config={"base_url": url, "token": token})
    api.register_channel(Platform.WEB.value, adapter)
    logger.info("mattermost plugin: registered for %s", url)
