"""Profile + browser-config resolution for the OpenClaw browser port (W0b).

Pull-based: every HTTP request that opts in re-resolves from the latest config
snapshot. No file watcher. See BRIEF-01-chrome-and-profiles.md.
"""

from __future__ import annotations

from .capabilities import BrowserProfileCapabilities, get_browser_profile_capabilities
from .config import (
    DEFAULT_OPENCLAW_BROWSER_COLOR,
    DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME,
    BrowserDriver,
    BrowserProfileConfig,
    BrowserProfileMode,
    ResolvedBrowserConfig,
    ResolvedBrowserProfile,
    SsrfPolicy,
)
from .resolver import resolve_browser_config, resolve_profile
from .service import (
    CreateProfileParams,
    CreateProfileResult,
    DeleteProfileResult,
    ProfileValidationError,
    allocate_cdp_port,
    allocate_color,
    create_profile,
    delete_profile,
    is_valid_profile_name,
)

__all__ = [
    "DEFAULT_OPENCLAW_BROWSER_COLOR",
    "DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME",
    "BrowserDriver",
    "BrowserProfileCapabilities",
    "BrowserProfileConfig",
    "BrowserProfileMode",
    "CreateProfileParams",
    "CreateProfileResult",
    "DeleteProfileResult",
    "ProfileValidationError",
    "ResolvedBrowserConfig",
    "ResolvedBrowserProfile",
    "SsrfPolicy",
    "allocate_cdp_port",
    "allocate_color",
    "create_profile",
    "delete_profile",
    "get_browser_profile_capabilities",
    "is_valid_profile_name",
    "resolve_browser_config",
    "resolve_profile",
]
