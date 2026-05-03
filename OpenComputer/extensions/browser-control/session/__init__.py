"""CDP attach + Playwright session lifecycle (Wave W1a).

Public surface:
  - connect_browser, force_disconnect_playwright_for_target
  - redact_cdp_url, CdpTimeouts (helpers)
  - PlaywrightSession (page + role-ref cache)
  - page_target_id (with /json/list HTTP fallback)
  - install_navigation_guard, NavigationGuardPolicy

Depends on profiles/ (SsrfPolicy types) and chrome/ (websocket URL discovery).
"""

from __future__ import annotations

from .cdp import (
    ConnectedBrowser,
    connect_browser,
    force_disconnect_playwright_for_target,
)
from .helpers import (
    CdpTimeouts,
    no_proxy_lease,
    normalize_cdp_http_base,
    normalize_cdp_url,
    redact_cdp_url,
    target_key,
)
from .nav_guard import (
    NAV_GUARD_BLOCKED_HOSTNAMES,
    NavigationBlocked,
    NavigationGuardPolicy,
    SsrfBlockedError,
    assert_browser_navigation_allowed,
    install_navigation_guard,
    is_policy_deny_error,
)
from .playwright_session import (
    MAX_ROLE_REFS_CACHE,
    PlaywrightSession,
    RoleRef,
    RoleRefsCacheEntry,
)
from .target_id import page_target_id

__all__ = [
    "MAX_ROLE_REFS_CACHE",
    "NAV_GUARD_BLOCKED_HOSTNAMES",
    "CdpTimeouts",
    "ConnectedBrowser",
    "NavigationBlocked",
    "NavigationGuardPolicy",
    "PlaywrightSession",
    "RoleRef",
    "RoleRefsCacheEntry",
    "SsrfBlockedError",
    "assert_browser_navigation_allowed",
    "connect_browser",
    "force_disconnect_playwright_for_target",
    "install_navigation_guard",
    "is_policy_deny_error",
    "no_proxy_lease",
    "normalize_cdp_http_base",
    "normalize_cdp_url",
    "page_target_id",
    "redact_cdp_url",
    "target_key",
]
