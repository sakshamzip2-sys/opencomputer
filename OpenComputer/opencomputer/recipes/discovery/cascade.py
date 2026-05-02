"""Auth-strategy cascade for a URL.

Tries strategies in order:

1. PUBLIC — plain GET, no auth headers, no cookies.
2. COOKIE — GET with cookies from the user's CDP-attached Chrome (if
   OPENCOMPUTER_BROWSER_CDP_URL is set; else skipped).
3. HEADER — GET with a generic browser User-Agent + Accept-Language.

Returns the first strategy that yields a 2xx response, plus the body.
On all-fail, returns ``None`` for the strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class CascadeResult:
    """Outcome of one cascade probe."""

    strategy: str | None  # "public" | "cookie" | "header" | None (all failed)
    status_code: int
    body: Any
    attempted: list[str]  # which strategies were tried


_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)


def run_cascade(url: str, *, timeout: float = 15.0) -> CascadeResult:
    """Probe ``url`` with PUBLIC → COOKIE → HEADER. Return first 2xx.

    On all-fail, ``strategy`` is None and ``status_code`` is the last
    failure's status (or 0 if no response was received).
    """
    attempted: list[str] = []

    # 1. PUBLIC
    attempted.append("public")
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=timeout)
        if 200 <= resp.status_code < 300:
            return CascadeResult(
                strategy="public",
                status_code=resp.status_code,
                body=_parse_body(resp),
                attempted=attempted,
            )
        last_status = resp.status_code
    except httpx.HTTPError:
        last_status = 0

    # 2. COOKIE — only if CDP attach is configured (else skip).
    import os

    if os.environ.get("OPENCOMPUTER_BROWSER_CDP_URL"):
        attempted.append("cookie")
        # Real implementation would extract cookies from the user's
        # Chrome via Playwright. v1 skeleton: skip this branch with a
        # log entry. The cascade still falls through to HEADER.
        # (Wiring requires CDP-aware cookie extraction; see Phase 5
        # next-session plan.)

    # 3. HEADER — generic browser-shaped headers.
    attempted.append("header")
    try:
        resp = httpx.get(
            url,
            follow_redirects=True,
            timeout=timeout,
            headers={
                "User-Agent": _DEFAULT_USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "application/json, text/plain, */*",
            },
        )
        if 200 <= resp.status_code < 300:
            return CascadeResult(
                strategy="header",
                status_code=resp.status_code,
                body=_parse_body(resp),
                attempted=attempted,
            )
        last_status = resp.status_code
    except httpx.HTTPError:
        pass

    return CascadeResult(
        strategy=None,
        status_code=last_status,
        body=None,
        attempted=attempted,
    )


def _parse_body(resp: httpx.Response) -> Any:
    """Best-effort body parse: JSON if Content-Type indicates, else text."""
    ct = resp.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return resp.text
    return resp.text
