"""DeepSeek provider plugin — registers DeepSeekProvider as 'deepseek'."""
from __future__ import annotations

from provider import DeepSeekProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("deepseek", DeepSeekProvider)
