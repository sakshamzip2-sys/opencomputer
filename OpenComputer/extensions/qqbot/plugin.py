"""QQ Bot plugin entry."""
from __future__ import annotations

from adapter import QQBotAdapter  # type: ignore[import-not-found]


def register(api) -> None:
    api.register_channel("qqbot", QQBotAdapter)
