"""GMI Cloud provider plugin — registers GMIProvider as 'gmi'."""
from __future__ import annotations

from provider import GMIProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("gmi", GMIProvider)
