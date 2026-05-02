"""OpenCode Go provider plugin — registers OpenCodeGoProvider as 'opencode-go'."""
from __future__ import annotations

from provider import OpenCodeGoProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("opencode-go", OpenCodeGoProvider)
