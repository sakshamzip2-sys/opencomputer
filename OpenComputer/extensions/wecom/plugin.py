"""WeCom (full corp app) plugin entry."""
from __future__ import annotations

from adapter import WeComFullAdapter  # type: ignore[import-not-found]


def register(api) -> None:
    api.register_channel("wecom", WeComFullAdapter)
