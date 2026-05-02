"""MiniMax provider plugin — registers MiniMaxProvider as 'minimax'."""
from __future__ import annotations

from provider import MiniMaxProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("minimax", MiniMaxProvider)
