"""Network-utility functions: SSRF guard, magic-byte sniff, proxy resolution.

Ported from gateway/platforms/base.py (Hermes 2026.4.23) with adaptations
for OpenComputer's plugin_sdk boundary. Pure stdlib + optional
``aiohttp_socks`` for SOCKS rDNS support.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import re
import socket
import subprocess
import sys
from typing import Any
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("plugin_sdk.network_utils")

# Magic-byte signatures for common image formats
_IMAGE_MAGIC_BYTES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
]
_WEBP_RIFF = b"RIFF"
_WEBP_TAG = b"WEBP"


def _looks_like_image(data: bytes) -> bool:
    """Magic-byte check: is this likely an image (not HTML masquerading as one)?

    Refuses empty or tiny payloads (< 8 bytes) as a defensive measure
    against truncated downloads. Detects PNG, JPEG, GIF (87a/89a), BMP,
    WEBP via canonical RIFF/WEBP framing.
    """
    if not data or len(data) < 8:
        return False
    for prefix, _ in _IMAGE_MAGIC_BYTES:
        if data.startswith(prefix):
            return True
    return (
        data[:4] == _WEBP_RIFF
        and len(data) >= 12
        and data[8:12] == _WEBP_TAG
    )


def safe_url_for_log(url: str, max_len: int = 200) -> str:
    """Strip userinfo/query/fragment for safe logging; truncate to max_len."""
    try:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            netloc = parsed.netloc.rsplit("@", 1)[-1]
            sanitized = urlunparse(
                (parsed.scheme, netloc, parsed.path, "", "", "")
            )
            return sanitized[:max_len]
    except (ValueError, AttributeError):
        pass
    return url[:max_len]


def is_network_accessible(host: str) -> bool:
    """Return False for loopback/private/link-local; True for routable hosts.

    Fail-closed on DNS resolution failure (returns False) — better to
    refuse than risk SSRF against an unknown host.
    """
    if not host:
        return False
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host.lower() == "localhost":
        return False
    # Try to parse as IP first
    try:
        addr = ipaddress.ip_address(host)
        return not (
            addr.is_loopback
            or addr.is_private
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        )
    except ValueError:
        pass
    # Resolve host
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, socket.gaierror):
        return False
    for _fam, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip)
            if (
                addr.is_loopback
                or addr.is_private
                or addr.is_link_local
                or addr.is_multicast
                or addr.is_reserved
                or addr.is_unspecified
            ):
                return False
        except ValueError:
            return False
    return True


def _detect_macos_system_proxy() -> str | None:
    """Read macOS system proxy via scutil --proxy."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.check_output(
            ["scutil", "--proxy"], stderr=subprocess.DEVNULL, timeout=2
        ).decode("utf-8", errors="ignore")
    except (OSError, subprocess.SubprocessError):
        return None
    https_enabled = re.search(r"HTTPSEnable\s*:\s*1", out)
    if not https_enabled:
        return None
    proxy_match = re.search(r"HTTPSProxy\s*:\s*(\S+)", out)
    port_match = re.search(r"HTTPSPort\s*:\s*(\d+)", out)
    if not (proxy_match and port_match):
        return None
    return f"http://{proxy_match.group(1)}:{port_match.group(1)}"


def resolve_proxy_url(env_var: str | None = None) -> str | None:
    """Resolve effective proxy URL.

    Priority:
    1. Per-platform env var (e.g. TELEGRAM_PROXY)
    2. HTTPS_PROXY / https_proxy
    3. HTTP_PROXY / http_proxy
    4. ALL_PROXY / all_proxy
    5. macOS system proxy via scutil (Darwin only)

    Returns None if nothing configured.
    """
    if env_var:
        v = os.environ.get(env_var)
        if v:
            return v
    for k in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        v = os.environ.get(k)
        if v:
            return v
    return _detect_macos_system_proxy()


def proxy_kwargs_for_aiohttp(url: str | None) -> dict[str, Any]:
    """Build kwargs for aiohttp.ClientSession to use ``url`` as proxy.

    HTTP/HTTPS: returns ``{"proxy": url}``.
    SOCKS: returns ``{"connector": ProxyConnector.from_url(url, rdns=True)}``
    if ``aiohttp_socks`` is installed; otherwise WARN and return ``{}``.
    """
    if not url:
        return {}
    if url.startswith(("http://", "https://")):
        return {"proxy": url}
    if url.startswith(("socks://", "socks4://", "socks5://", "socks5h://")):
        try:
            from aiohttp_socks import ProxyConnector  # type: ignore[import-not-found]

            return {"connector": ProxyConnector.from_url(url, rdns=True)}
        except ImportError:
            logger.warning(
                "SOCKS proxy requested but aiohttp_socks not installed; ignoring"
            )
            return {}
    return {}


def proxy_kwargs_for_bot(url: str | None) -> dict[str, Any]:
    """Build kwargs for python-telegram-bot/discord.py Bot constructors.

    Same shape as :func:`proxy_kwargs_for_aiohttp`; aliased here so call
    sites that wire a Bot rather than a raw aiohttp session read clearly.
    """
    return proxy_kwargs_for_aiohttp(url)


async def ssrf_redirect_guard(response: Any) -> None:
    """httpx async response hook: re-validate each redirect target.

    Usage:
        client = httpx.AsyncClient(
            event_hooks={"response": [ssrf_redirect_guard]}
        )
    """
    status = getattr(response, "status_code", None)
    if status in (301, 302, 303, 307, 308):
        location = None
        with contextlib.suppress(AttributeError, KeyError):
            location = response.headers.get("location")
        if location:
            parsed = urlparse(location)
            host = parsed.hostname
            if host and not is_network_accessible(host):
                raise RuntimeError(
                    f"SSRF guard: refused redirect to private host {host!r}"
                )


# Backward-compatibility alias: the symbol was originally exported with a
# leading underscore. Keep the old name for any external wiring that imports
# it. Prefer the public ``ssrf_redirect_guard`` for new code.
_ssrf_redirect_guard = ssrf_redirect_guard


__all__ = [
    "is_network_accessible",
    "proxy_kwargs_for_aiohttp",
    "proxy_kwargs_for_bot",
    "resolve_proxy_url",
    "safe_url_for_log",
    "ssrf_redirect_guard",
]
