"""Kimi China provider plugin — registers KimiChinaProvider as 'kimi-cn'."""
from __future__ import annotations

from provider import KimiChinaProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("kimi-cn", KimiChinaProvider)
