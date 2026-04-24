"""OpenCLI Scraper plugin — entry module.

Phase C2: The plugin code is on disk and testable in isolation.
Tools are NOT registered with the global ToolRegistry — registration
waits for Session A's Phase 4 (ConsentGate + SignalNormalizer wiring).

See design doc §14 for the split between C2 and Phase 4 responsibilities.
"""

from __future__ import annotations

import logging

from plugin_sdk.core import PluginManifest

log = logging.getLogger(__name__)

MANIFEST = PluginManifest(
    id="opencli-scraper",
    name="OpenCLI Scraper",
    version="0.1.0",
    description=(
        "Wraps OpenCLI for safe, consented web scraping. "
        "15 curated adapters with rate limiting and robots.txt enforcement."
    ),
    author="OpenComputer Contributors",
    license="Apache-2.0",
    kind="tools",
    entry="plugin",
    enabled_by_default=False,
    tool_names=("ScrapeRaw", "FetchProfile", "MonitorPage"),
)


def register(api) -> None:  # PluginAPI is duck-typed
    """Plugin registration entry point.

    Phase C2: Returns immediately without registering tools. Session A
    wires ConsentGate + SignalNormalizer in Phase 4, then adds the
    api.register_tool() calls here.
    """
    log.info(
        "[opencli-scraper] awaiting Phase 4 integration — "
        "tools not registered (ConsentGate + SignalNormalizer required)"
    )
    # Phase 4 TODO: uncomment after Session A integration
    # from wrapper import OpenCLIWrapper
    # from rate_limiter import RateLimiter
    # from robots_cache import RobotsCache
    # from tools import ScrapeRawTool, FetchProfileTool, MonitorPageTool
    # wrapper = OpenCLIWrapper()
    # rate_limiter = RateLimiter()
    # robots_cache = RobotsCache()
    # api.register_tool(ScrapeRawTool(wrapper=wrapper, rate_limiter=rate_limiter, robots_cache=robots_cache))
    # api.register_tool(FetchProfileTool(wrapper=wrapper, rate_limiter=rate_limiter, robots_cache=robots_cache))
    # api.register_tool(MonitorPageTool(wrapper=wrapper, rate_limiter=rate_limiter, robots_cache=robots_cache))
    return
