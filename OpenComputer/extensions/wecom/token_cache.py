"""WeCom (qyapi.weixin.qq.com) access_token rotation.

WeCom uses a different host than the public-account Weixin API and a
slightly different credential triple — corp_id (corpid) + secret
(corpsecret) — but the same 2-hour-expiry rotation pattern.

Endpoint:
  GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=...&corpsecret=...

Response:
  {"errcode": 0, "errmsg": "ok", "access_token": "...", "expires_in": 7200}
"""
from __future__ import annotations

import threading
import time
from typing import Any

import httpx

TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
DEFAULT_TIMEOUT_SECONDS = 20.0
REFRESH_SKEW_SECONDS = 60.0


def fetch_access_token(*, corp_id: str, secret: str) -> tuple[str, float]:
    """GET /cgi-bin/gettoken; return (access_token, expires_at_unix_seconds)."""
    response = httpx.get(
        TOKEN_URL,
        params={"corpid": corp_id, "corpsecret": secret},
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"WeCom gettoken returned {response.status_code}: "
            f"{response.text[:200]}"
        )
    data: Any = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"WeCom gettoken returned non-dict: {data!r}")
    if int(data.get("errcode", 0) or 0) != 0:
        raise RuntimeError(
            f"WeCom gettoken error {data.get('errcode')}: "
            f"{data.get('errmsg', 'unknown')}"
        )
    token = str(data.get("access_token") or "")
    expires_in = int(data.get("expires_in") or 7200)
    if not token:
        raise RuntimeError(f"WeCom gettoken returned no access_token: {data!r}")
    return token, time.time() + expires_in


class WeComTokenCache:
    """Thread-safe cache for one (corp_id, secret) pair."""

    def __init__(self, *, corp_id: str, secret: str) -> None:
        self._corp_id = corp_id
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
            token, expires_at = fetch_access_token(
                corp_id=self._corp_id, secret=self._secret
            )
            self._access_token = token
            self._expires_at = expires_at
            return token


__all__ = [
    "REFRESH_SKEW_SECONDS",
    "TOKEN_URL",
    "WeComTokenCache",
    "fetch_access_token",
]
