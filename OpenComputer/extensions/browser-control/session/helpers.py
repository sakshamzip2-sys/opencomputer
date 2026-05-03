"""Shared helpers for the session subsystem.

  redact_cdp_url    strip user:pass from a CDP URL before logging (with a
                    safe fallback for unparseable input).
  CdpTimeouts       centralised timeout constants + per-profile clamp
                    (loopback gets shorter HTTP timeout / WS handshake).
  normalize_cdp_url strip trailing slash + lower-case scheme/host. The
                    in-flight dedup map and connect cache key on this.
  normalize_cdp_http_base
                    coerce ws/wss → http/https, strip /devtools/browser/<id>
                    and trailing /cdp so /json/list works for direct-WS URLs.
  target_key        compose `<normalized_cdp_url>::<target_id>` for the
                    blocked-target set + role-ref FIFO cache.
  no_proxy_lease    reference-counted async context manager that prepends
                    `localhost,127.0.0.1,[::1]` to NO_PROXY when the URL is
                    loopback. Restores only if our value is still in place.

The proxy-bypass lease is the *correctness* story for concurrent connects:
two callers racing into the same loopback URL share a lease (count=2 at
peak) so the inner connect doesn't see a NO_PROXY snapshot from a sibling
that already restored.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit, urlunsplit

# ─── timeout constants ────────────────────────────────────────────────

CDP_HTTP_REQUEST_TIMEOUT_MS: Final[int] = 1500
CDP_WS_HANDSHAKE_TIMEOUT_MS: Final[int] = 5000
CDP_JSON_NEW_TIMEOUT_MS: Final[int] = 1500

# Per-profile clamps for the existing-session profile (where we're racing
# with a real user typing in their browser).
PROFILE_HTTP_REACHABILITY_TIMEOUT_MS: Final[int] = 300
PROFILE_WS_REACHABILITY_MIN_TIMEOUT_MS: Final[int] = 200
PROFILE_WS_REACHABILITY_MAX_TIMEOUT_MS: Final[int] = 2000
PROFILE_ATTACH_RETRY_TIMEOUT_MS: Final[int] = 1200


@dataclass(frozen=True, slots=True)
class CdpTimeouts:
    """Timeout bundle resolved per profile.

    `http_timeout_ms` covers `/json/version`, `/json/list`, `/json/new`.
    `ws_handshake_timeout_ms` covers connect_over_cdp / raw WS open.
    """

    http_timeout_ms: int = CDP_HTTP_REQUEST_TIMEOUT_MS
    ws_handshake_timeout_ms: int = CDP_WS_HANDSHAKE_TIMEOUT_MS

    @classmethod
    def for_profile(
        cls,
        *,
        is_loopback: bool,
        remote_http_timeout_ms: int = CDP_HTTP_REQUEST_TIMEOUT_MS,
        remote_handshake_timeout_ms: int = CDP_WS_HANDSHAKE_TIMEOUT_MS,
        override_http_ms: int | None = None,
    ) -> CdpTimeouts:
        if is_loopback:
            http = override_http_ms or PROFILE_HTTP_REACHABILITY_TIMEOUT_MS
            ws = max(
                PROFILE_WS_REACHABILITY_MIN_TIMEOUT_MS,
                min(PROFILE_WS_REACHABILITY_MAX_TIMEOUT_MS, http * 2),
            )
            return cls(http_timeout_ms=http, ws_handshake_timeout_ms=ws)
        http = max(remote_http_timeout_ms, override_http_ms or 0)
        ws = max(remote_handshake_timeout_ms, http * 2)
        return cls(http_timeout_ms=http, ws_handshake_timeout_ms=ws)


# ─── url helpers ──────────────────────────────────────────────────────


def normalize_cdp_url(raw: str) -> str:
    """Strip trailing slash. The dedup map keys on this exact string.

    We intentionally do NOT lowercase host or canonicalize port — the
    upstream config resolver already produced the canonical form, and
    accepting alternate spellings would defeat the dedup contract."""
    if not raw:
        return raw
    return raw.rstrip("/")


def normalize_cdp_http_base(cdp_url: str) -> str:
    """Coerce ws/wss → http/https, strip `/devtools/browser/<id>` and `/cdp`.

    Used so `/json/list` HTTP fallback works even when the input is a
    direct-WS URL like `ws://host:port/devtools/browser/<uuid>`.
    """
    if not cdp_url:
        return cdp_url
    try:
        parts = urlsplit(cdp_url)
    except ValueError:
        return cdp_url
    scheme = parts.scheme
    if scheme == "ws":
        scheme = "http"
    elif scheme == "wss":
        scheme = "https"
    elif scheme not in ("http", "https"):
        return cdp_url

    path = parts.path
    # Strip `/devtools/browser/<anything>`.
    devtools_idx = path.lower().find("/devtools/browser/")
    if devtools_idx >= 0:
        path = path[:devtools_idx]
    # Strip a trailing /cdp (or /cdp/).
    while path.endswith("/cdp") or path.endswith("/cdp/"):
        path = path.rstrip("/")
        if path.endswith("/cdp"):
            path = path[: -len("/cdp")]
    return urlunsplit((scheme, parts.netloc, path, parts.query, parts.fragment))


def target_key(cdp_url: str, target_id: str) -> str:
    """Composite key for blocked-target / role-ref maps.

    `::` is load-bearing — prefix scans use `<normalized>::` to enumerate
    all target ids for a given CDP URL.
    """
    return f"{normalize_cdp_url(cdp_url)}::{target_id}"


def redact_cdp_url(cdp_url: str | None) -> str | None:
    """Strip user:pass before logging.

    Falls back to an opaque `<unparseable cdp url>` placeholder if `urlsplit`
    raises — we never echo upstream-controlled text into logs unredacted.
    """
    if cdp_url is None:
        return None
    if not cdp_url:
        return cdp_url
    try:
        parts = urlsplit(cdp_url)
    except ValueError:
        return "<unparseable cdp url>"
    if parts.username is None and parts.password is None:
        return cdp_url
    host = parts.hostname or ""
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# ─── NO_PROXY reference-counted lease ─────────────────────────────────

_NO_PROXY_VARS: tuple[str, ...] = ("NO_PROXY", "no_proxy")
_LOOPBACK_TOKENS: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]")


def _is_loopback_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost":
        return True
    if host.startswith("127."):
        return True
    return host == "::1"


def _no_proxy_already_covers_loopback() -> bool:
    for var in _NO_PROXY_VARS:
        cur = os.environ.get(var, "")
        if cur and all(token in cur for token in _LOOPBACK_TOKENS):
            return True
    return False


@dataclass(slots=True)
class _LeaseState:
    count: int = 0
    snapshot: dict[str, str | None] = None  # type: ignore[assignment]
    applied: dict[str, str] = None  # type: ignore[assignment]


_lease = _LeaseState()
_lease_mutex = asyncio.Lock()


@contextlib.asynccontextmanager
async def no_proxy_lease(url: str) -> AsyncIterator[Literal[True, False]]:
    """Reference-counted NO_PROXY scope.

    No-ops for non-loopback URLs (yields False) and for cases where
    NO_PROXY already covers loopback. Otherwise prepends the loopback
    tokens to NO_PROXY/no_proxy. Restores only if the env still holds
    our applied value — if external code mutated it mid-flight, we
    leave the user's value alone.
    """
    if not _is_loopback_url(url):
        yield False
        return

    if _no_proxy_already_covers_loopback():
        yield False
        return

    async with _lease_mutex:
        _lease.count += 1
        if _lease.count == 1:
            _lease.snapshot = {var: os.environ.get(var) for var in _NO_PROXY_VARS}
            applied: dict[str, str] = {}
            for var in _NO_PROXY_VARS:
                cur = os.environ.get(var, "")
                token_str = ",".join(_LOOPBACK_TOKENS)
                new_value = token_str if not cur else f"{token_str},{cur}"
                os.environ[var] = new_value
                applied[var] = new_value
            _lease.applied = applied

    try:
        yield True
    finally:
        async with _lease_mutex:
            _lease.count -= 1
            if _lease.count == 0:
                snapshot = _lease.snapshot or {}
                applied = _lease.applied or {}
                for var, prior in snapshot.items():
                    if os.environ.get(var) != applied.get(var):
                        # External mutation — leave it alone.
                        continue
                    if prior is None:
                        os.environ.pop(var, None)
                    else:
                        os.environ[var] = prior
                _lease.snapshot = None  # type: ignore[assignment]
                _lease.applied = None  # type: ignore[assignment]
