"""Slack channel plugin — entry point.

Outbound-only via Web API. Inbound: use Slack Outgoing Webhooks → OC
webhook adapter (G.3). See ``adapter.py`` docstring for full setup.

Env var: ``SLACK_BOT_TOKEN`` (starts ``xoxb-``). Disabled by default.
"""

from __future__ import annotations

import logging
import os

try:
    from adapter import SlackAdapter  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.slack.adapter import SlackAdapter  # package mode

from plugin_sdk.core import Platform

logger = logging.getLogger("opencomputer.ext.slack")


def register(api) -> None:  # PluginAPI duck-typed
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        logger.info("slack plugin: not registering (SLACK_BOT_TOKEN unset)")
        return
    if not token.startswith("xoxb-"):
        logger.warning(
            "slack plugin: SLACK_BOT_TOKEN doesn't start with 'xoxb-' "
            "(should be a Bot User OAuth Token, not user/app token)"
        )
    adapter = SlackAdapter(config={"bot_token": token})
    api.register_channel(Platform.SLACK.value, adapter)
    logger.info("slack plugin: registered")
