"""Telegram IP-fallback transport — for users in geo-blocked regions.

Sticky-IP retry preserving Host header + TLS SNI = api.telegram.org.
DoH discovery via Google + Cloudflare; seed IP 149.154.167.220.

Use case: users in regions where ``api.telegram.org`` is DNS-blocked or
IP-routed to a sinkhole. Setting ``TELEGRAM_FALLBACK_IPS=auto`` discovers
fresh A records via DNS-over-HTTPS at ``connect()`` time; setting it to
a comma-separated list of IPs uses those directly. On a connect failure
to the system-resolved host, the transport tries each fallback IP in
turn, rewriting the URL host while keeping the Host header + TLS SNI
extension pointed at ``api.telegram.org`` so certificate validation +
virtual-hosting still succeed.

Once an IP succeeds, it becomes "sticky" for subsequent requests to
avoid paying the failover cost on every call. On a sticky-IP failure
we bust and retry the original host, then fall through the list.

Default behaviour (env unset) is unchanged: a regular httpx client
with no fallback. The whole module is opt-in.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Any

import httpx

logger = logging.getLogger("opencomputer.ext.telegram.network")

_SEED_IP = "149.154.167.220"
_DOH_ENDPOINTS = (
    "https://dns.google/resolve",
    "https://cloudflare-dns.com/dns-query",
)


class TelegramFallbackTransport(httpx.AsyncBaseTransport):
    """Sticky-IP retry httpx transport.

    On request: try the system-resolved host first. On connect failure,
    try fallback IPs in order, rewriting the URL host to IP and setting
    Host header + TLS SNI extension to ``api.telegram.org``.
    """

    def __init__(
        self,
        fallback_ips: list[str],
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._fallback_ips = list(fallback_ips)
        self._sticky_ip: str | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._sticky_ip:
            req = self._rewrite_request_for_ip(request, self._sticky_ip)
            try:
                return await self._inner.handle_async_request(req)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # Sticky IP went bad — bust and fall through to the
                # full retry chain so we rediscover a working route.
                self._sticky_ip = None

        # Try the original host first (system DNS resolution).
        try:
            return await self._inner.handle_async_request(request)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            logger.warning(
                "telegram primary host failed (%s); trying fallback IPs", exc
            )

        # Try fallback IPs in order; first success becomes sticky.
        for ip in self._fallback_ips:
            req = self._rewrite_request_for_ip(request, ip)
            try:
                resp = await self._inner.handle_async_request(req)
                self._sticky_ip = ip
                logger.info("telegram fallback IP %s succeeded; sticky", ip)
                return resp
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                logger.debug("telegram fallback IP %s failed: %s", ip, exc)

        raise httpx.ConnectError("all fallback IPs exhausted")

    def _rewrite_request_for_ip(
        self, request: httpx.Request, ip: str
    ) -> httpx.Request:
        from copy import copy

        new_url = request.url.copy_with(host=ip)
        new_headers = copy(request.headers)
        new_headers["host"] = "api.telegram.org"
        # ``content`` may be ``None`` for GETs; passing ``None`` works.
        new_request = httpx.Request(
            request.method,
            new_url,
            headers=new_headers,
            content=request.content,
        )
        # Tell httpx (and the underlying TLS layer) to use the original
        # hostname for SNI even though the URL host is now an IP — without
        # this the cert chain wouldn't validate.
        new_request.extensions["sni_hostname"] = "api.telegram.org"
        return new_request

    async def aclose(self) -> None:
        await self._inner.aclose()


async def discover_fallback_ips() -> list[str]:
    """Async DoH discovery from Google + Cloudflare.

    Returns the union of valid IPv4 A records from both providers,
    sorted for stable ordering. Falls back to the well-known seed IP
    if neither resolver responds with usable data (network down,
    upstream blocked, etc.).
    """
    discovered: set[str] = set()
    async with httpx.AsyncClient(timeout=5.0) as client:
        for endpoint in _DOH_ENDPOINTS:
            try:
                params = {"name": "api.telegram.org", "type": "A"}
                headers = {"Accept": "application/dns-json"}
                resp = await client.get(endpoint, params=params, headers=headers)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                for answer in data.get("Answer", []):
                    if answer.get("type") == 1:  # A record
                        ip = str(answer.get("data", "")).strip()
                        validated = parse_fallback_ip(ip)
                        if validated:
                            discovered.add(validated)
            except Exception:  # noqa: BLE001 — DoH is best-effort
                continue
    if not discovered:
        return [_SEED_IP]
    return sorted(discovered)


def parse_fallback_ip(value: str) -> str | None:
    """Validate IPv4 (no IPv6 / private / loopback / link-local).

    Returns the normalised IP string or ``None`` for any rejected
    input. Belt-and-braces filtering: we reject every reserved /
    non-routable category so a malicious DoH response can't trick us
    into hitting an internal-network address.
    """
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    if not isinstance(addr, ipaddress.IPv4Address):
        return None
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return None
    return str(addr)


def parse_fallback_ip_env(value: str) -> list[str]:
    """Parse the ``TELEGRAM_FALLBACK_IPS`` env value.

    - Empty / unset → ``[]``: caller does not enable fallback.
    - ``"auto"`` (case-insensitive) → ``[]``: caller is expected to
      invoke :func:`discover_fallback_ips` instead.
    - Comma-separated IPs → list of validated IPs (invalid entries
      silently dropped).
    """
    if not value or value.strip().lower() == "auto":
        return []
    out: list[str] = []
    for part in value.split(","):
        ip = parse_fallback_ip(part.strip())
        if ip:
            out.append(ip)
    return out


def is_auto_mode(value: str | None) -> bool:
    """``True`` iff the env value is ``"auto"`` (case-insensitive)."""
    return bool(value) and value.strip().lower() == "auto"


__all__ = [
    "TelegramFallbackTransport",
    "discover_fallback_ips",
    "is_auto_mode",
    "parse_fallback_ip",
    "parse_fallback_ip_env",
]
