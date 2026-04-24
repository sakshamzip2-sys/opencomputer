"""Per-domain token-bucket rate limiter.

Design doc §7 defaults are encoded in ``DEFAULT_LIMITS``. The ``*`` entry
is the fallback for unknown domains.

Implementation notes
--------------------
* Uses a simple in-memory counter + per-domain asyncio.Lock.
* ``acquire(domain)`` blocks until a token is available by sleeping in a
  short loop — suitable for the low-concurrency scraping use-case.
* Per-call rate is per-INVOCATION of the wrapper, NOT per underlying HTTP
  request that OpenCLI makes internally. Document this limitation to callers.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

# (count, period_seconds) — tokens available per period.
DEFAULT_LIMITS: dict[str, tuple[int, int]] = {
    "github.com": (60, 3600),  # 60 per hour
    "reddit.com": (60, 60),  # 60 per minute
    "linkedin.com": (30, 60),  # conservative
    "x.com": (30, 60),  # twitter, conservative
    "news.ycombinator.com": (60, 60),  # generous
    "stackoverflow.com": (60, 60),
    "youtube.com": (60, 60),
    "medium.com": (60, 60),
    "bsky.app": (60, 60),
    "arxiv.org": (60, 60),
    "wikipedia.org": (200, 60),  # MediaWiki API is generous
    "producthunt.com": (60, 60),
    "*": (30, 60),  # default for unknown domains
}

_POLL_INTERVAL_S = 0.05  # 50 ms granularity when waiting for a token


class _DomainBucket:
    """Token-bucket state for one domain."""

    __slots__ = ("count", "period_s", "lock", "used", "window_start")

    def __init__(self, count: int, period_s: int) -> None:
        self.count = count
        self.period_s = period_s
        self.lock: asyncio.Lock = asyncio.Lock()
        self.used: int = 0
        self.window_start: float = time.monotonic()


class RateLimiter:
    """Per-domain token-bucket rate limiter.

    Parameters
    ----------
    defaults:
        Override map ``{domain: (count, period_seconds)}``. Merged on top of
        ``DEFAULT_LIMITS`` — per-domain overrides win; ``"*"`` entry overrides
        the global fallback.
    """

    def __init__(
        self,
        *,
        defaults: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self._limits: dict[str, tuple[int, int]] = {**DEFAULT_LIMITS}
        if defaults:
            self._limits.update(defaults)

        self._buckets: dict[str, _DomainBucket] = {}
        self._registry_lock = asyncio.Lock()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def acquire(self, domain: str) -> None:
        """Block until one token is available for *domain*.

        The domain is normalised by stripping ``www.`` and port numbers before
        lookup, falling back to the ``"*"`` default if no specific entry exists.
        """
        normalised = _normalise_domain(domain)
        bucket = await self._get_or_create(normalised)

        while True:
            async with bucket.lock:
                now = time.monotonic()
                # Reset window if the current period has expired.
                if now - bucket.window_start >= bucket.period_s:
                    bucket.used = 0
                    bucket.window_start = now

                if bucket.used < bucket.count:
                    bucket.used += 1
                    log.debug(
                        "rate_limiter: acquired token for %r (%d/%d in window)",
                        normalised,
                        bucket.used,
                        bucket.count,
                    )
                    return

                wait_remaining = bucket.period_s - (now - bucket.window_start)
                log.debug(
                    "rate_limiter: throttled on %r — sleeping %.2fs",
                    normalised,
                    _POLL_INTERVAL_S,
                )

            # Release lock before sleeping so other coroutines can proceed.
            _ = wait_remaining  # informational only
            await asyncio.sleep(_POLL_INTERVAL_S)

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _get_or_create(self, domain: str) -> _DomainBucket:
        """Return (or lazily create) the bucket for *domain*."""
        if domain in self._buckets:
            return self._buckets[domain]

        async with self._registry_lock:
            # Double-check after acquiring lock.
            if domain in self._buckets:
                return self._buckets[domain]

            count, period_s = self._limits.get(domain) or self._limits.get("*", (30, 60))
            self._buckets[domain] = _DomainBucket(count=count, period_s=period_s)
            log.debug(
                "rate_limiter: created bucket for %r (%d/%ds)", domain, count, period_s
            )
            return self._buckets[domain]


def _normalise_domain(domain: str) -> str:
    """Strip scheme, port, and leading ``www.`` from a domain or URL."""
    # Handle full URLs passed by accident.
    if "://" in domain:
        domain = domain.split("://", 1)[1]
    # Strip path.
    domain = domain.split("/")[0]
    # Strip port.
    domain = domain.split(":")[0]
    # Strip www. prefix.
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.lower()


__all__ = ["RateLimiter", "DEFAULT_LIMITS"]
