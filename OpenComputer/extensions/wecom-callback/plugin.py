"""WeCom Callback channel plugin entry."""
from __future__ import annotations

from adapter import WeComCallbackAdapter  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_channel("wecom-callback", WeComCallbackAdapter)
