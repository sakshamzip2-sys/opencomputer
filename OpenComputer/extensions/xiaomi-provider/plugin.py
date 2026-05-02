"""Xiaomi MiMo provider plugin — registers XiaomiProvider as 'xiaomi'."""
from __future__ import annotations

from provider import XiaomiProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("xiaomi", XiaomiProvider)
