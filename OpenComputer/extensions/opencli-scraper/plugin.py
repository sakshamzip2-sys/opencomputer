# enabled_by_default flip awaits user's legal review per PR-2 spec
"""OpenCLI Scraper plugin — entry module.

Phase 4 wiring (PR-2 of 2026-04-25 Hermes parity plan):
    - Tools are registered through PluginAPI. The agent loop's ConsentGate
      enforces capability_claims before dispatch.
    - Successful scrapes publish WebObservationEvent to the F2 default_bus
      for downstream subscribers (audit log, evolution trajectory, etc.).
    - plugin.json still has enabled_by_default: false — the user flips it
      after their legal review. Do NOT change plugin.json here.

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
    """Phase 4 wiring (PR-2 of 2026-04-25 Hermes parity plan):
    - tools register through PluginAPI (the agent loop's ConsentGate
      enforces capability_claims before dispatch)
    - successful scrapes publish to the F2 bus (default_bus)
    - manifest still has enabled_by_default: false — user flips it
      after their legal review.
    """
    import importlib.util
    import sys
    from pathlib import Path

    # Ensure the plugin's own directory is importable so the local sibling
    # modules (wrapper, rate_limiter, robots_cache, field_whitelist, …)
    # resolve correctly when called from the top-level loader.  We add it
    # temporarily — the loader itself may have already done this; we only
    # remove it if WE were the ones who added it (tracked by _path_added).
    _plugin_dir = str(Path(__file__).parent)
    _path_added = _plugin_dir not in sys.path
    if _path_added:
        sys.path.insert(0, _plugin_dir)

    try:
        from rate_limiter import RateLimiter  # type: ignore[import-not-found]
        from robots_cache import RobotsCache  # type: ignore[import-not-found]
        from wrapper import OpenCLIWrapper  # type: ignore[import-not-found]

        # Import tools under a qualified name so sys.modules["tools"] is NOT
        # set to our plugin-local tools.py.  A bare "tools" entry in
        # sys.modules would shadow the "tools/" sub-package created by the
        # plugin-scaffold smoke-check, causing it to fail when run in the same
        # process (e.g. tests/test_phase12b2_plugin_scaffold.py).
        _tools_qname = "extensions.opencli_scraper.tools"
        if _tools_qname not in sys.modules:
            _spec = importlib.util.spec_from_file_location(
                _tools_qname,
                str(Path(__file__).parent / "tools.py"),
            )
            _mod = importlib.util.module_from_spec(_spec)
            _mod.__package__ = "extensions.opencli_scraper"
            sys.modules[_tools_qname] = _mod
            _spec.loader.exec_module(_mod)
        _tools_mod = sys.modules[_tools_qname]
        FetchProfileTool = _tools_mod.FetchProfileTool
        MonitorPageTool = _tools_mod.MonitorPageTool
        ScrapeRawTool = _tools_mod.ScrapeRawTool
    finally:
        if _path_added and _plugin_dir in sys.path:
            sys.path.remove(_plugin_dir)

    # Construct shared infrastructure once; all three tools share the same
    # wrapper, rate_limiter, and robots_cache instances.
    wrapper = OpenCLIWrapper()
    rate_limiter = RateLimiter()
    robots_cache = RobotsCache()

    for tool_cls in (ScrapeRawTool, FetchProfileTool, MonitorPageTool):
        tool = tool_cls(
            wrapper=wrapper,
            rate_limiter=rate_limiter,
            robots_cache=robots_cache,
        )
        api.register_tool(tool)

    log.info(
        "[opencli-scraper] Phase 4 registration complete — "
        "3 tools registered (ConsentGate enforces capability_claims at dispatch); "
        "enabled_by_default=false until user's legal review"
    )
