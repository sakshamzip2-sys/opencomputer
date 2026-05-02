"""Nous Portal provider plugin — registers NousPortalProvider as 'nous-portal'."""
from __future__ import annotations

from provider import NousPortalProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("nous-portal", NousPortalProvider)
