"""Tencent TokenHub provider plugin — registers TencentProvider as 'tencent'."""
from __future__ import annotations

from provider import TencentProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("tencent", TencentProvider)
