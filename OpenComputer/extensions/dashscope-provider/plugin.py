"""DashScope provider plugin — registers DashScopeProvider as 'dashscope'."""
from __future__ import annotations

from provider import DashScopeProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("dashscope", DashScopeProvider)
