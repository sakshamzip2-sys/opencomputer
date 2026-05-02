"""OpenCode Zen provider plugin — registers OpenCodeZenProvider as 'opencode-zen'."""
from __future__ import annotations

from provider import OpenCodeZenProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("opencode-zen", OpenCodeZenProvider)
