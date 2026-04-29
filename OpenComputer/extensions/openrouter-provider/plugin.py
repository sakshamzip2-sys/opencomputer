"""OpenRouter provider plugin — registers OpenRouterProvider as 'openrouter'."""
from __future__ import annotations

from provider import OpenRouterProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("openrouter", OpenRouterProvider)
