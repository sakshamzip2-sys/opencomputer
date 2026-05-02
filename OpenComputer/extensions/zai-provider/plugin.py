"""Z.AI / GLM provider plugin — registers ZAIProvider as 'zai'."""
from __future__ import annotations

from provider import ZAIProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("zai", ZAIProvider)
