"""Weixin access_token rotation.

Weixin Public Account access tokens expire in 7200 seconds. The Customer
Service Message API (and most others) require a fresh token in the URL
query string. This module implements:

  - ``fetch_access_token(appid, secret)`` — POST to /cgi-bin/token, parse
    {"access_token": "...", "expires_in": N}, raise on errcode != 0
  - ``WeixinTokenCache`` — instance-cached token with 60s skew refresh

Weixin imposes a global rate limit on /cgi-bin/token (a few hundred req/day
per appid), so caching aggressively is mandatory — every send mustn't hit
the token endpoint. The cache holds one token in memory; for persistence
across restarts, use a central token service (out of scope for v1).
"""
from __future__ import annotations

import threading
import time
from typing import Any

import httpx

TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
DEFAULT_TIMEOUT_SECONDS = 20.0
REFRESH_SKEW_SECONDS = 60.0


def fetch_access_token(*, appid: str, secret: str) -> tuple[str, float]:
    """POST to Weixin /cgi-bin/token; return (access_token, expires_at_unix_seconds).

    Raises ``RuntimeError`` if Weixin returns ``errcode != 0`` or HTTP non-200.
    """
    response = httpx.get(
        TOKEN_URL,
        params={
            "grant_type": "client_credential",
            "appid": appid,
            "secret": secret,
        },
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Weixin token endpoint returned {response.status_code}: "
            f"{response.text[:200]}"
        )
    data: Any = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Weixin token endpoint returned non-dict: {data!r}")
    if int(data.get("errcode", 0) or 0) != 0:
        raise RuntimeError(
            f"Weixin token error {data.get('errcode')}: "
            f"{data.get('errmsg', 'unknown')}"
        )
    token = str(data.get("access_token") or "")
    expires_in = int(data.get("expires_in") or 7200)
    if not token:
        raise RuntimeError(f"Weixin token endpoint returned no access_token: {data!r}")
    return token, time.time() + expires_in


class WeixinTokenCache:
    """Thread-safe access_token cache for one (appid, secret) pair.

    Refreshes 60 seconds before stated expiry so concurrent sends never
    race against an expiring token.
    """

    def __init__(self, *, appid: str, secret: str) -> None:
        self._appid = appid
        self._secret = secret
        self._access_token: str = ""
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def get_access_token(self) -> str:
        """Return a non-expiring token, refreshing on the lock if needed."""
        # Fast path — no lock, no refresh
        if self._access_token and time.time() + REFRESH_SKEW_SECONDS < self._expires_at:
            return self._access_token

        with self._lock:
            # Re-check inside the lock — another thread may have refreshed
            if self._access_token and time.time() + REFRESH_SKEW_SECONDS < self._expires_at:
                return self._access_token
            token, expires_at = fetch_access_token(
                appid=self._appid, secret=self._secret
            )
            self._access_token = token
            self._expires_at = expires_at
            return token


__all__ = [
    "REFRESH_SKEW_SECONDS",
    "TOKEN_URL",
    "WeixinTokenCache",
    "fetch_access_token",
]
