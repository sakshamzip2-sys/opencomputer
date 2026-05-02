"""QQ Bot Open Platform access_token rotation.

Endpoint:
  POST https://bots.qq.com/app/getAppAccessToken
  Body: {"appId": "...", "clientSecret": "..."}
  Response: {"access_token": "...", "expires_in": 7200}

Unlike Weixin/WeCom, the credential body is JSON (not query params), and
the host is ``bots.qq.com`` — distinct from the API host
``api.sgroup.qq.com`` used for actual messages.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import httpx

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
DEFAULT_TIMEOUT_SECONDS = 20.0
REFRESH_SKEW_SECONDS = 60.0


def fetch_bot_token(*, appid: str, secret: str) -> tuple[str, float]:
    """POST credentials, return (access_token, expires_at_unix_seconds)."""
    response = httpx.post(
        TOKEN_URL,
        json={"appId": appid, "clientSecret": secret},
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"QQ Bot getAppAccessToken returned {response.status_code}: "
            f"{response.text[:200]}"
        )
    data: Any = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(
            f"QQ Bot getAppAccessToken returned non-dict: {data!r}"
        )
    token = str(data.get("access_token") or "")
    expires_in = int(data.get("expires_in") or 7200)
    if not token:
        raise RuntimeError(
            f"QQ Bot getAppAccessToken returned no access_token: {data!r}"
        )
    return token, time.time() + expires_in


class QQBotTokenCache:
    """Thread-safe cache for one (appid, secret) pair."""

    def __init__(self, *, appid: str, secret: str) -> None:
        self._appid = appid
        self._secret = secret
        self._access_token: str = ""
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def get_access_token(self) -> str:
        if self._access_token and time.time() + REFRESH_SKEW_SECONDS < self._expires_at:
            return self._access_token
        with self._lock:
            if self._access_token and time.time() + REFRESH_SKEW_SECONDS < self._expires_at:
                return self._access_token
            token, expires_at = fetch_bot_token(
                appid=self._appid, secret=self._secret
            )
            self._access_token = token
            self._expires_at = expires_at
            return token


__all__ = [
    "REFRESH_SKEW_SECONDS",
    "TOKEN_URL",
    "QQBotTokenCache",
    "fetch_bot_token",
]
