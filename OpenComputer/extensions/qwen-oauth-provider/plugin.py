"""Qwen OAuth provider plugin."""
from __future__ import annotations

from provider import QwenOAuthProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("qwen-oauth", QwenOAuthProvider)
