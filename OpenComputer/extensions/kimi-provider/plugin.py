"""Kimi (Moonshot) provider plugin — registers KimiProvider as 'kimi'."""
from __future__ import annotations

from provider import KimiProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("kimi", KimiProvider)
