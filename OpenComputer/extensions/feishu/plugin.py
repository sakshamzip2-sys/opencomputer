"""Feishu/Lark channel plugin entry."""
from __future__ import annotations

from adapter import FeishuAdapter  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_channel("feishu", FeishuAdapter)
