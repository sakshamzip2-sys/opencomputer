"""The three OpenCLI Scraper tools.

``ScrapeRawTool``, ``FetchProfileTool``, ``MonitorPageTool`` are NOT
registered with the global ToolRegistry in Phase C2. Session A wires
them in Phase 4 after ConsentGate + SignalNormalizer are ready.

Each tool takes the three collaborators (``OpenCLIWrapper``,
``RateLimiter``, ``RobotsCache``) as constructor arguments so tests can
inject mocks without touching the filesystem or network.

Execute flow:
    1. Rate-limit acquire for the target domain.
    2. Robots.txt allow check for the target URL.
    3. Spawn opencli subprocess via the wrapper.
    4. Filter output through the per-adapter field whitelist.
    5. Return ``ToolResult``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from urllib.parse import urlparse

from field_whitelist import filter_output  # type: ignore[import-not-found]
from rate_limiter import RateLimiter  # type: ignore[import-not-found]
from robots_cache import RobotsCache  # type: ignore[import-not-found]
from wrapper import OpenCLIWrapper  # type: ignore[import-not-found]

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

log = logging.getLogger(__name__)

# ── Platform → adapter mapping for FetchProfileTool ───────────────────────────

_PLATFORM_ADAPTER: dict[str, str] = {
    "github": "github/user",
    "reddit": "reddit/user",
    "linkedin": "linkedin/timeline",
    "twitter": "twitter/profile",
    "hackernews": "hackernews/user",
    "hn": "hackernews/user",
    "stackoverflow": "stackoverflow/user",
    "so": "stackoverflow/user",
    "youtube": "youtube/user",
    "medium": "medium/user",
    "bluesky": "bluesky/profile",
    "arxiv": "arxiv/search",
    "wikipedia": "wikipedia/user-contributions",
    "producthunt": "producthunt/user",
    "ph": "producthunt/user",
}

# ── Domain extraction helpers ──────────────────────────────────────────────────

_ADAPTER_DOMAINS: dict[str, str] = {
    "github/user": "github.com",
    "reddit/user": "reddit.com",
    "reddit/posts": "reddit.com",
    "reddit/comments": "reddit.com",
    "linkedin/timeline": "linkedin.com",
    "twitter/profile": "x.com",
    "twitter/tweets": "x.com",
    "hackernews/user": "news.ycombinator.com",
    "stackoverflow/user": "stackoverflow.com",
    "youtube/user": "youtube.com",
    "medium/user": "medium.com",
    "bluesky/profile": "bsky.app",
    "arxiv/search": "arxiv.org",
    "wikipedia/user-contributions": "wikipedia.org",
    "producthunt/user": "producthunt.com",
}


def _domain_for_adapter(adapter: str) -> str:
    """Return the canonical domain for *adapter*, falling back to adapter name."""
    return _ADAPTER_DOMAINS.get(adapter, adapter)


def _domain_for_url(url: str) -> str:
    """Extract the netloc from a URL string."""
    parsed = urlparse(url)
    return parsed.netloc or url


# ── ScrapeRawTool ──────────────────────────────────────────────────────────────


class ScrapeRawTool(BaseTool):
    """Low-level: invoke any whitelisted adapter directly with raw args.

    Bypasses the platform-alias layer — the caller must know the exact
    adapter slug (e.g. ``"github/user"``).
    """

    parallel_safe = True

    def __init__(
        self,
        wrapper: OpenCLIWrapper,
        rate_limiter: RateLimiter,
        robots_cache: RobotsCache,
    ) -> None:
        self._wrapper = wrapper
        self._rate_limiter = rate_limiter
        self._robots_cache = robots_cache

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="ScrapeRaw",
            description=(
                "Invoke an OpenCLI adapter directly and return filtered output.\n\n"
                "The ``adapter`` must be one of the 15 supported slugs "
                "(e.g. ``github/user``, ``reddit/posts``, ``twitter/profile``). "
                "``args`` are passed verbatim to the adapter (e.g. ``[\"octocat\"]``)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "adapter": {
                        "type": "string",
                        "description": (
                            "Adapter slug, e.g. 'github/user', 'reddit/posts'."
                        ),
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Positional arguments forwarded to the adapter.",
                        "default": [],
                    },
                },
                "required": ["adapter"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        adapter = call.arguments.get("adapter", "")
        args: list[str] = call.arguments.get("args", [])

        if not adapter:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: 'adapter' is required",
                is_error=True,
            )

        domain = _domain_for_adapter(adapter)
        url = f"https://{domain}/"

        return await _execute_scrape(
            call_id=call.id,
            adapter=adapter,
            args=args,
            url=url,
            domain=domain,
            wrapper=self._wrapper,
            rate_limiter=self._rate_limiter,
            robots_cache=self._robots_cache,
        )


# ── FetchProfileTool ───────────────────────────────────────────────────────────


class FetchProfileTool(BaseTool):
    """High-level: fetch a user profile from a known platform.

    Maps a human-friendly ``platform`` name to the underlying adapter slug,
    then delegates to the same execute path as ``ScrapeRawTool``.
    """

    parallel_safe = True

    def __init__(
        self,
        wrapper: OpenCLIWrapper,
        rate_limiter: RateLimiter,
        robots_cache: RobotsCache,
    ) -> None:
        self._wrapper = wrapper
        self._rate_limiter = rate_limiter
        self._robots_cache = robots_cache

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="FetchProfile",
            description=(
                "Fetch a user profile from a supported platform.\n\n"
                f"Supported platforms: {', '.join(sorted(_PLATFORM_ADAPTER))}.\n"
                "Returns only whitelisted profile fields."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "description": (
                            "Platform name, e.g. 'github', 'twitter', 'linkedin'."
                        ),
                    },
                    "user": {
                        "type": "string",
                        "description": "Username or handle to look up.",
                    },
                },
                "required": ["platform", "user"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        platform = call.arguments.get("platform", "").lower().strip()
        user = call.arguments.get("user", "").strip()

        if not platform:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: 'platform' is required",
                is_error=True,
            )
        if not user:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: 'user' is required",
                is_error=True,
            )

        adapter = _PLATFORM_ADAPTER.get(platform)
        if adapter is None:
            supported = ", ".join(sorted(_PLATFORM_ADAPTER))
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: unknown platform {platform!r}. Supported: {supported}",
                is_error=True,
            )

        domain = _domain_for_adapter(adapter)
        url = f"https://{domain}/{user}"

        return await _execute_scrape(
            call_id=call.id,
            adapter=adapter,
            args=[user],
            url=url,
            domain=domain,
            wrapper=self._wrapper,
            rate_limiter=self._rate_limiter,
            robots_cache=self._robots_cache,
        )


# ── MonitorPageTool ────────────────────────────────────────────────────────────


class MonitorPageTool(BaseTool):
    """Monitor a URL for changes (Phase C2: single fetch + content hash).

    Real polling (repeated at ``interval_s``) is deferred to Phase C4
    (``content_monitoring`` module). For C2, this does ONE fetch and
    returns the content hash + timestamp so callers can compare across
    invocations.
    """

    parallel_safe = True

    def __init__(
        self,
        wrapper: OpenCLIWrapper,
        rate_limiter: RateLimiter,
        robots_cache: RobotsCache,
    ) -> None:
        self._wrapper = wrapper
        self._rate_limiter = rate_limiter
        self._robots_cache = robots_cache

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="MonitorPage",
            description=(
                "Fetch a URL and return a content hash + timestamp.\n\n"
                "Phase C2: performs a single fetch only. "
                "Compare the returned ``content_hash`` across invocations "
                "to detect changes. ``interval_s`` is reserved for Phase C4 polling."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch.",
                    },
                    "interval_s": {
                        "type": "integer",
                        "description": (
                            "Polling interval in seconds (reserved for Phase C4). "
                            "Ignored in C2."
                        ),
                        "default": 300,
                    },
                },
                "required": ["url"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        url = call.arguments.get("url", "").strip()
        if not url:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: 'url' is required",
                is_error=True,
            )

        domain = _domain_for_url(url)

        # Rate-limit + robots check.
        await self._rate_limiter.acquire(domain)
        allowed = await self._robots_cache.allowed(url)
        if not allowed:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: robots.txt disallows fetching {url!r}",
                is_error=True,
            )

        # For a generic URL we use a generic adapter that just fetches content.
        # In practice callers would use ScrapeRaw with the appropriate adapter;
        # MonitorPage's niche is page-level change detection. We map the domain
        # to the closest known adapter, or fall back to the raw fetch approach.
        adapter = _ADAPTER_DOMAINS_REVERSE.get(domain, "")
        try:
            if adapter:
                raw = await self._wrapper.run(adapter, url)
            else:
                # Fall back: use the wrapper's generic mode with the URL as arg.
                raw = await self._wrapper.run("", url)
        except Exception as exc:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error fetching {url!r}: {exc}",
                is_error=True,
            )

        raw_str = json.dumps(raw, sort_keys=True)
        content_hash = hashlib.sha256(raw_str.encode()).hexdigest()
        fetched_at = time.time()

        result = {
            "url": url,
            "content_hash": content_hash,
            "fetched_at": fetched_at,
            "note": "Phase C2: single fetch only. Real polling available in Phase C4.",
        }
        return ToolResult(
            tool_call_id=call.id,
            content=json.dumps(result),
        )


# Reverse map for MonitorPageTool domain → adapter lookup.
_ADAPTER_DOMAINS_REVERSE: dict[str, str] = {v: k for k, v in _ADAPTER_DOMAINS.items()}


# ── Shared execute helper ──────────────────────────────────────────────────────


async def _execute_scrape(
    *,
    call_id: str,
    adapter: str,
    args: list[str],
    url: str,
    domain: str,
    wrapper: OpenCLIWrapper,
    rate_limiter: RateLimiter,
    robots_cache: RobotsCache,
) -> ToolResult:
    """Shared execute path: rate-limit → robots → run → filter → return."""
    # 1. Rate limit.
    await rate_limiter.acquire(domain)

    # 2. Robots check.
    allowed = await robots_cache.allowed(url)
    if not allowed:
        return ToolResult(
            tool_call_id=call_id,
            content=f"Error: robots.txt disallows fetching {url!r} (adapter: {adapter!r})",
            is_error=True,
        )

    # 3. Subprocess.
    try:
        raw = await wrapper.run(adapter, *args)
    except Exception as exc:
        return ToolResult(
            tool_call_id=call_id,
            content=f"Error running adapter {adapter!r}: {exc}",
            is_error=True,
        )

    # 4. Field whitelist filter.
    # raw may be wrapped in {ok, data} envelope — unwrap data if present.
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    filtered = filter_output(adapter, data)

    return ToolResult(
        tool_call_id=call_id,
        content=json.dumps(filtered),
    )


__all__ = ["ScrapeRawTool", "FetchProfileTool", "MonitorPageTool"]
