"""Arcee AI provider plugin — registers ArceeProvider as 'arcee'."""
from __future__ import annotations

from provider import ArceeProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("arcee", ArceeProvider)
