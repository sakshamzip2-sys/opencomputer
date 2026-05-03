"""Loopback-host detection + auth-header injection.

Auth headers are attached ONLY when the target URL is loopback. Cross-host
calls deliberately get no auth — defense in depth against SSRF leaking the
bearer token. Caller-supplied auth headers are never overwritten.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, MutableMapping
from urllib.parse import urlsplit

from ..server.auth import BrowserAuth

_AUTH_HEADER = "authorization"
_PASSWORD_HEADER = "x-opencomputer-password"


def is_loopback_host(host: str) -> bool:
    """True if ``host`` is a loopback address.

    Accepts IPv4 (127.0.0.0/8), IPv6 (``::1``), the IPv4-mapped IPv6
    form (``::ffff:127.0.0.1``), and the literal ``localhost``.
    """
    if not host:
        return False
    h = host.strip().lower()
    if h == "localhost":
        return True
    # Strip surrounding brackets for IPv6 in URL form ([::1])
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    # IPv4-mapped IPv6: ::ffff:127.0.0.1 ipaddress.is_loopback returns False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return False


def is_loopback_url(url: str) -> bool:
    """True iff ``url`` is an absolute http(s) URL pointing at loopback."""
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https"):
        return False
    return is_loopback_host(parts.hostname or "")


def inject_auth_headers(
    headers: Mapping[str, str] | None,
    *,
    auth: BrowserAuth | None,
    url: str,
) -> dict[str, str]:
    """Return a new headers dict with auth attached on loopback targets only.

    - If a caller already supplied Authorization or X-OpenComputer-Password,
      it is left untouched (callers win).
    - If ``url`` is not absolute-http loopback, no auth is injected.
    - Token mode preferred (Authorization: Bearer ...) when both available.
    """
    out: MutableMapping[str, str] = dict(headers or {})

    if _has_header(out, _AUTH_HEADER) or _has_header(out, _PASSWORD_HEADER):
        return dict(out)

    if not is_loopback_url(url):
        return dict(out)

    if auth is None:
        return dict(out)

    if auth.token:
        out["Authorization"] = f"Bearer {auth.token}"
    elif auth.password:
        out["X-OpenComputer-Password"] = auth.password

    return dict(out)


def _has_header(headers: Mapping[str, str], name: str) -> bool:
    target = name.lower()
    return any(k.lower() == target for k in headers)


__all__ = [
    "BrowserAuth",
    "inject_auth_headers",
    "is_loopback_host",
    "is_loopback_url",
]
