"""Auto-fetch URLs from incoming messages — Tier B item 19.

Saksham forwards article links and chart-image URLs to Telegram all day.
Today the agent has to (a) notice the URL is there, (b) decide it should
fetch, (c) call ``WebFetch`` — three round-trips of model reasoning before
the user gets a useful answer.

This module pre-fetches those URLs **before the agent loop even runs** and
injects a short summary into the prompt. The agent sees the URL content
inline alongside the user's message, so it can respond on the first turn
instead of doing the look-and-fetch dance.

Design notes:

- **Fetch reuses the existing ``WebFetchTool``** — one URL fetcher, one
  set of tests, one place to fix bugs. We don't reimplement HTML
  scraping.

- **SSRF guard** rejects private / link-local / loopback / cloud-metadata
  IPs. Auto-fetching whatever URL appears in chat is a real attack
  surface — a malicious sender could direct the agent at
  ``http://169.254.169.254/latest/meta-data/`` and exfiltrate AWS
  instance role credentials.

- **Per-session URL cache** so re-asks within a session don't refetch.
  Bounded — capped at 64 URLs per session.

- **Configurable** via ``LinkUnderstandingConfig``: ``enabled`` (default
  on), ``max_urls_per_message`` (default 3), ``per_url_max_chars``
  (default 1500). Disable globally for privacy-conscious users via
  ``opencomputer.agent.link_understanding.DEFAULT_CONFIG.enabled = False``
  or per-profile in ``config.yaml``.

- **Cross-channel uniform**: works for Telegram, Discord, CLI, gateway —
  any path that goes through the agent loop's injection engine. The
  injection provider runs whenever a user message contains URLs.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field
from typing import ClassVar
from urllib.parse import urlparse

logger = logging.getLogger("opencomputer.agent.link_understanding")


# ──────────────────────────────────────────────────────────────────────
# URL extraction
# ──────────────────────────────────────────────────────────────────────


# Conservative URL regex — http/https only, requires scheme + host. Stops at
# whitespace and a few common closing punctuation chars (so trailing dots /
# commas / parens don't get glued to the URL).
_URL_RE = re.compile(
    r"https?://[^\s<>()\[\]{}\"'`,]+(?<![.,;:!?])",
    re.IGNORECASE,
)


def extract_urls(text: str, *, max_urls: int = 3) -> list[str]:
    """Extract up to ``max_urls`` HTTP(S) URLs from ``text``.

    Order-preserving (the first URL the user typed comes first). Duplicates
    are deduped while preserving first-seen order. Trailing punctuation
    that crept into the regex match is stripped.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:!?\"'`")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= max_urls:
            break
    return out


# ──────────────────────────────────────────────────────────────────────
# SSRF guard
# ──────────────────────────────────────────────────────────────────────


# Cloud metadata endpoints that any auto-fetch path MUST refuse. AWS
# 169.254.169.254 also covers GCP / DigitalOcean since they all squat on
# the same link-local IP, but Azure uses a different one — list both.
_CLOUD_METADATA_HOSTS = frozenset({
    "169.254.169.254",
    "169.254.170.2",  # AWS Fargate
    "metadata.google.internal",
    "metadata.azure.com",
})


def _is_blocked_ip(ip_str: str) -> bool:
    """Return True if ``ip_str`` is private / loopback / link-local / multicast."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_safe_url(url: str) -> bool:
    """Validate ``url`` for auto-fetch.

    Refuse any URL that:

    - isn't http(s);
    - resolves to a private / loopback / link-local / cloud-metadata IP;
    - has no host component;
    - is a literal cloud metadata hostname (case-insensitive).

    DNS resolution happens here. We accept the latency cost (one A-record
    lookup) because the alternative — racing the fetch — opens a
    DNS-rebinding hole where the same name resolves to a public IP at
    check time and a private IP at fetch time. This isn't bulletproof
    (multi-A records still race), but mitigates the obvious attack.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip()
    if not host:
        return False
    if host.lower() in _CLOUD_METADATA_HOSTS:
        return False

    # If host is already an IP literal, check directly.
    if _is_blocked_ip(host):
        return False

    # Otherwise resolve. Block if ANY resolved IP is unsafe — defends
    # against split-horizon DNS where a public-looking name returns
    # private addresses.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Can't resolve — let the caller decide. Default: refuse.
        return False
    for family, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        if _is_blocked_ip(ip):
            return False
    return True


# ──────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────


@dataclass
class LinkUnderstandingConfig:
    """Per-instance settings. Mutable so tests / runtime overrides apply."""

    #: Master switch. Set to False to disable auto-fetch entirely.
    enabled: bool = True

    #: Most URLs to fetch from a single user message. Caps the worst-case
    #: latency for chatty users with link-heavy messages.
    max_urls_per_message: int = 3

    #: Per-URL fetch character cap. Tight to keep prompt-token cost
    #: predictable. The agent can call ``WebFetch`` directly with a
    #: larger cap if it needs the full text.
    per_url_max_chars: int = 1500

    #: Per-fetch timeout (seconds).
    timeout_s: float = 10.0

    #: How many URLs we cache per session (LRU-ish — drop oldest entry).
    cache_max_per_session: int = 64


#: Module-level default — reachable via
#: ``opencomputer.agent.link_understanding.DEFAULT_CONFIG``. Mutating
#: this changes behavior for every freshly-constructed provider.
DEFAULT_CONFIG = LinkUnderstandingConfig()


# ──────────────────────────────────────────────────────────────────────
# Per-session cache
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _SessionCache:
    """Bounded URL → fetched text cache, one per session id."""

    entries: dict[str, str] = field(default_factory=dict)
    max_size: int = 64

    def get(self, url: str) -> str | None:
        return self.entries.get(url)

    def put(self, url: str, text: str) -> None:
        if len(self.entries) >= self.max_size:
            # Drop oldest insertion-order entry. dict ordering is FIFO
            # since Py3.7, so popping the first key is the LRU-ish move.
            try:
                first_key = next(iter(self.entries))
                self.entries.pop(first_key)
            except StopIteration:
                pass
        self.entries[url] = text


_session_caches: dict[str, _SessionCache] = {}


def _cache_for(session_id: str, max_size: int) -> _SessionCache:
    cache = _session_caches.get(session_id)
    if cache is None:
        cache = _SessionCache(max_size=max_size)
        _session_caches[session_id] = cache
    return cache


def _clear_caches() -> None:
    """Test helper — drop all per-session caches."""
    _session_caches.clear()


# ──────────────────────────────────────────────────────────────────────
# Fetcher (delegates to WebFetchTool)
# ──────────────────────────────────────────────────────────────────────


class LinkFetcher:
    """Thin wrapper over ``WebFetchTool`` so injection providers don't
    need to know about ``ToolCall`` / ``ToolResult`` shapes.

    Reuses the exact same HTTP path as the agent's manual ``WebFetch``
    calls — no parallel scraping codepath to maintain.
    """

    _SHARED: ClassVar[LinkFetcher | None] = None

    def __init__(self) -> None:
        from opencomputer.tools.web_fetch import WebFetchTool

        self._tool = WebFetchTool()

    @classmethod
    def shared(cls) -> LinkFetcher:
        """Module-level singleton. Tests may instantiate fresh ones."""
        if cls._SHARED is None:
            cls._SHARED = cls()
        return cls._SHARED

    async def fetch(
        self, url: str, *, max_chars: int, timeout_s: float
    ) -> str | None:
        """Return fetched body text, or ``None`` on any error.

        Errors are swallowed silently — auto-fetch is best-effort. A
        404 / timeout / DNS failure on one URL must not break the
        injection for the other URLs in the same message.
        """
        from plugin_sdk.core import ToolCall

        call = ToolCall(
            id="link-understanding-auto",
            name="WebFetch",
            arguments={
                "url": url,
                "max_chars": max_chars,
                "timeout_s": timeout_s,
            },
        )
        try:
            result = await self._tool.execute(call)
        except Exception:  # noqa: BLE001 — best-effort fetch
            logger.warning("link_understanding: fetch raised for %r", url, exc_info=True)
            return None
        if result.is_error:
            logger.info("link_understanding: fetch error for %r — %s", url, result.content)
            return None
        return result.content


__all__ = [
    "DEFAULT_CONFIG",
    "LinkFetcher",
    "LinkUnderstandingConfig",
    "_clear_caches",
    "extract_urls",
    "is_safe_url",
]
