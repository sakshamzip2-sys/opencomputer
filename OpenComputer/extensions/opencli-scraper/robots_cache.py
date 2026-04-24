"""24h TTL cache for robots.txt enforcement.

Behaviour
---------
* 404 on robots.txt  → **allow** (no restrictions per RFC 9309 §2.3.1.2)
* 5xx on robots.txt  → **deny** (treat as deliberate block; conservative)
* Other network error → **deny** (fail-safe)
* Cache TTL          → 86400 seconds (24 hours)
* Uses ``urllib.robotparser.RobotFileParser`` from stdlib.
* HTTP fetch via ``httpx.AsyncClient`` (already in project deps).
  Falls back to ``urllib.request`` + ``asyncio.to_thread`` if httpx is
  unavailable (should not happen in practice).

User-Agent used for both robots.txt fetch and path permission checks.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

log = logging.getLogger(__name__)

USER_AGENT = "OpenComputer-OpenCLIScraper/0.1"
_CACHE_TTL_S = 86400  # 24 hours


@dataclass
class _CacheEntry:
    parser: urllib.robotparser.RobotFileParser
    fetched_at: float = field(default_factory=time.monotonic)


class RobotsCache:
    """Async robots.txt cache with 24-hour TTL.

    Usage
    -----
    >>> cache = RobotsCache()
    >>> allowed = await cache.allowed("https://example.com/some/path")
    """

    def __init__(self) -> None:
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def allowed(self, url: str) -> bool:
        """Return True if our User-Agent is allowed to fetch *url*.

        Parameters
        ----------
        url:
            The full URL to check (scheme + host + path).

        Returns
        -------
        bool
            ``True`` if fetching is permitted, ``False`` otherwise.
        """
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path
        # Reconstruct robots URL.
        scheme = parsed.scheme or "https"
        robots_url = f"{scheme}://{domain}/robots.txt"

        parser = await self._fetch_or_cached(domain, robots_url)
        if parser is None:
            # Deny on fetch failure (conservative).
            return False
        result: bool = parser.can_fetch(USER_AGENT, url)
        log.debug("robots_cache: %s → %s for %r", domain, "allow" if result else "deny", url)
        return result

    async def _fetch_or_cached(
        self, domain: str, robots_url: str
    ) -> urllib.robotparser.RobotFileParser | None:
        """Return a cached (or freshly fetched) ``RobotFileParser`` for *domain*.

        Returns ``None`` on irrecoverable fetch error (5xx / network error).
        Returns an empty parser that allows everything on 404.
        """
        # Fast path — no lock needed if fresh entry exists.
        now = time.monotonic()
        entry = self._cache.get(domain)
        if entry is not None and (now - entry.fetched_at) < _CACHE_TTL_S:
            return entry.parser

        # Per-domain lock to prevent thundering-herd duplicate fetches.
        async with self._global_lock:
            if domain not in self._locks:
                self._locks[domain] = asyncio.Lock()
            domain_lock = self._locks[domain]

        async with domain_lock:
            # Re-check inside lock.
            now = time.monotonic()
            entry = self._cache.get(domain)
            if entry is not None and (now - entry.fetched_at) < _CACHE_TTL_S:
                return entry.parser

            parser = await self._fetch_robots(robots_url)
            if parser is not None:
                self._cache[domain] = _CacheEntry(parser=parser, fetched_at=time.monotonic())
            return parser

    async def _fetch_robots(
        self, robots_url: str
    ) -> urllib.robotparser.RobotFileParser | None:
        """Fetch and parse a robots.txt file.

        Returns
        -------
        RobotFileParser | None
            Parsed parser on success or 404.
            ``None`` on 5xx or network error.
        """
        try:
            import httpx  # already in project deps

            async with httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=10.0,
            ) as client:
                response = await client.get(robots_url)
                return self._parse_response(robots_url, response.status_code, response.text)

        except ImportError:
            # Fallback: use urllib in a thread.
            log.debug("httpx not available; falling back to urllib for robots.txt")
            return await asyncio.to_thread(self._fetch_robots_sync, robots_url)

        except Exception as exc:  # network error, DNS failure, etc.
            log.warning("robots_cache: error fetching %r: %s — denying", robots_url, exc)
            return None

    def _parse_response(
        self, robots_url: str, status_code: int, body: str
    ) -> urllib.robotparser.RobotFileParser | None:
        """Convert an HTTP response into a ``RobotFileParser``.

        404 → allow-all parser.
        5xx → None (caller will deny).
        Other non-200 → allow-all (conservative assumption: no restrictions).
        200 → parse body.
        """
        if status_code == 404:
            log.debug("robots_cache: 404 for %r → allow all", robots_url)
            rp = urllib.robotparser.RobotFileParser()
            rp.allow_all = True  # type: ignore[attr-defined]
            return rp

        if 500 <= status_code < 600:
            log.warning(
                "robots_cache: %d on %r — denying (possible deliberate block)",
                status_code,
                robots_url,
            )
            return None

        # 200 or any other non-4xx: parse body.
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        rp.parse(body.splitlines())
        return rp

    def _fetch_robots_sync(self, robots_url: str) -> urllib.robotparser.RobotFileParser | None:
        """Synchronous fallback for robots.txt fetch (runs in a thread pool)."""
        import urllib.error
        import urllib.request

        try:
            req = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return self._parse_response(robots_url, resp.status, body)
        except urllib.error.HTTPError as exc:
            return self._parse_response(robots_url, exc.code, "")
        except Exception as exc:
            log.warning("robots_cache: sync fetch error for %r: %s", robots_url, exc)
            return None


__all__ = ["RobotsCache", "USER_AGENT"]
