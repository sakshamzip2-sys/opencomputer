"""Webhook Inbound (multi-platform) plugin entry."""
from __future__ import annotations

from adapter import WebhookInboundAdapter  # type: ignore[import-not-found]


def register(api) -> None:
    # WebhookInboundAdapter.__init__ requires a config dict (matches the
    # BaseChannelAdapter contract used by telegram/slack/etc.). An empty
    # dict is valid — host/port fall back to the adapter's DEFAULT_* class
    # attributes. Passing nothing raised TypeError and the plugin failed
    # to register at all.
    api.register_channel("webhook-inbound", WebhookInboundAdapter(config={}))
