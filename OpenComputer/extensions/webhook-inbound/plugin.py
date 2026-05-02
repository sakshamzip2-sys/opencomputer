"""Webhook Inbound (multi-platform) plugin entry."""
from __future__ import annotations

from adapter import WebhookInboundAdapter  # type: ignore[import-not-found]


def register(api) -> None:
    api.register_channel("webhook-inbound", WebhookInboundAdapter)
