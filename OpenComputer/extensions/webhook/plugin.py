"""Webhook channel plugin — entry point.

Registers the WebhookAdapter so the gateway starts an HTTP listener on
``WEBHOOK_HOST:WEBHOOK_PORT`` (defaults: 127.0.0.1:18790). Tokens are
managed via the ``opencomputer webhook`` CLI subcommand.

Per ``plugin.json``: ``enabled_by_default: false`` — must be explicitly
enabled in the active profile because it opens an inbound network port.
"""

from __future__ import annotations

import logging
import os

# Plugin-loader mode: sibling modules are importable by plain name.
# Package mode (e.g. `pytest extensions.webhook.adapter`): falls through.
try:
    from adapter import WebhookAdapter  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.webhook.adapter import WebhookAdapter  # package mode

from plugin_sdk.core import Platform

logger = logging.getLogger("opencomputer.ext.webhook")


def register(api) -> None:  # PluginAPI duck-typed
    config = {
        "host": os.environ.get("WEBHOOK_HOST", "127.0.0.1"),
        "port": int(os.environ.get("WEBHOOK_PORT", "18790")),
    }
    adapter = WebhookAdapter(config=config)
    # PR 3c.5: hand the PluginAPI to the adapter so its deliver_only
    # branch can reach ``api.outgoing_queue.enqueue`` without
    # re-importing gateway internals (preserves the plugin_sdk →
    # opencomputer one-way boundary).
    adapter.bind_plugin_api(api)
    api.register_channel(Platform.WEBHOOK.value, adapter)
    logger.info(
        "webhook: registered (host=%s port=%d) — bind on gateway start",
        config["host"], config["port"],
    )
