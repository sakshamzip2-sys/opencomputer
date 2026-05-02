"""Weixin (WeChat Public Account) plugin entry."""
from __future__ import annotations

from adapter import WeixinAdapter  # type: ignore[import-not-found]


def register(api) -> None:
    api.register_channel("weixin", WeixinAdapter)
