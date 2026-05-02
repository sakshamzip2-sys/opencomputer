"""xAI provider plugin — registers XAIProvider as 'xai'."""
from __future__ import annotations

from provider import XAIProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("xai", XAIProvider)
