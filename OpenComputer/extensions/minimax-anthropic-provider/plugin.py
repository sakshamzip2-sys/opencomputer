"""MiniMax (Anthropic-shaped) provider plugin."""
from __future__ import annotations

from provider import MiniMaxAnthropicProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("minimax", MiniMaxAnthropicProvider)
