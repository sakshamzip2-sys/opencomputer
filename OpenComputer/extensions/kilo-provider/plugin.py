"""Kilo Code provider plugin — registers KiloProvider as 'kilo'."""
from __future__ import annotations

from provider import KiloProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("kilo", KiloProvider)
