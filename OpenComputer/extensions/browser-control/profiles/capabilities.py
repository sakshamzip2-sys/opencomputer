"""Derive runtime capability bits from a resolved profile.

Pure mapping — see deep-dive truth table:

    driver == "existing-session"   -> local-existing-session  (uses_chrome_mcp)
    !cdp_is_loopback               -> remote-cdp              (persistent playwright)
    else                            -> local-managed           (everything supported)

Used by snapshot/, server_context/, and tools_core/ to switch codepaths.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import BrowserProfileMode, ResolvedBrowserProfile


@dataclass(frozen=True, slots=True)
class BrowserProfileCapabilities:
    mode: BrowserProfileMode
    is_remote: bool
    uses_chrome_mcp: bool
    uses_persistent_playwright: bool
    uses_control_extension: bool
    supports_per_tab_ws: bool
    supports_json_tab_endpoints: bool
    supports_reset: bool
    supports_managed_tab_limit: bool


def get_browser_profile_capabilities(profile: ResolvedBrowserProfile) -> BrowserProfileCapabilities:
    if profile.driver == "existing-session":
        return BrowserProfileCapabilities(
            mode="local-existing-session",
            is_remote=False,
            uses_chrome_mcp=True,
            uses_persistent_playwright=False,
            uses_control_extension=False,
            supports_per_tab_ws=False,
            supports_json_tab_endpoints=False,
            supports_reset=False,
            supports_managed_tab_limit=False,
        )
    if profile.driver == "control-extension":
        # Wave 6: extension-based control. The user's real Chrome (or
        # the managed Chrome with --load-extension on Track 1) attaches
        # to our daemon WS endpoint and drives tabs via chrome.debugger.
        # No managed subprocess; no chrome-devtools-mcp.
        return BrowserProfileCapabilities(
            mode="local-control-extension",
            is_remote=False,
            uses_chrome_mcp=False,
            uses_persistent_playwright=False,
            uses_control_extension=True,
            supports_per_tab_ws=False,
            supports_json_tab_endpoints=False,
            supports_reset=False,
            supports_managed_tab_limit=False,
        )
    if not profile.cdp_is_loopback:
        return BrowserProfileCapabilities(
            mode="remote-cdp",
            is_remote=True,
            uses_chrome_mcp=False,
            uses_persistent_playwright=True,
            uses_control_extension=False,
            supports_per_tab_ws=False,
            supports_json_tab_endpoints=False,
            supports_reset=False,
            supports_managed_tab_limit=False,
        )
    return BrowserProfileCapabilities(
        mode="local-managed",
        is_remote=False,
        uses_chrome_mcp=False,
        uses_persistent_playwright=False,
        uses_control_extension=False,
        supports_per_tab_ws=True,
        supports_json_tab_endpoints=True,
        supports_reset=True,
        supports_managed_tab_limit=True,
    )
