"""Vercel AI Gateway plugin — registers VercelAIGatewayProvider as 'ai-gateway'."""
from __future__ import annotations

from provider import VercelAIGatewayProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("ai-gateway", VercelAIGatewayProvider)
