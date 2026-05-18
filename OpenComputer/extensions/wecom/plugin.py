"""WeCom (full corp app) plugin entry."""
from __future__ import annotations

import os

from adapter import WeComFullAdapter  # type: ignore[import-not-found]


def register(api) -> None:
    # Credential guard (mirrors telegram/signal). Registering a channel
    # adapter with no credentials crashes the gateway at startup with
    # connect_returned_false. Defense-in-depth behind the load_all
    # credential gate.
    if not all(
        os.environ.get(var, "").strip()
        for var in ("WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_SECRET")
    ):
        return
    api.register_channel("wecom", WeComFullAdapter())
