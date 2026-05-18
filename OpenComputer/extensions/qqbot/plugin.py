"""QQ Bot plugin entry."""
from __future__ import annotations

import os

from adapter import QQBotAdapter  # type: ignore[import-not-found]


def register(api) -> None:
    # Credential guard (mirrors telegram/signal). Registering a channel
    # adapter with no credentials crashes the gateway at startup with
    # connect_returned_false. Defense-in-depth behind the load_all
    # credential gate.
    if not all(
        os.environ.get(var, "").strip()
        for var in ("QQBOT_APPID", "QQBOT_SECRET")
    ):
        return
    api.register_channel("qqbot", QQBotAdapter())
