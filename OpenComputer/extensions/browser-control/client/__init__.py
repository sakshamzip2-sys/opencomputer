"""Client transport — HTTP + in-process dispatcher fork.

Public surface:
  - fetch_browser_json: dual-transport request helper
  - BrowserActions: thin wrappers over the HTTP routes
  - BrowserAuth: credentials passed to fetch_browser_json
  - BrowserServiceError: re-export
  - tab_registry helpers + form-field + proxy-files utilities
"""

from __future__ import annotations

from .actions import BrowserActions
from .auth import (
    inject_auth_headers,
    is_loopback_host,
    is_loopback_url,
)
from .fetch import (
    Transport,
    fetch_browser_json,
    set_default_dispatcher_app,
)
from .form_fields import normalize_form_field
from .proxy_files import (
    apply_proxy_paths,
    persist_proxy_files,
)
from .tab_registry import (
    TrackedTab,
    close_tracked_browser_tabs_for_sessions,
    count_tracked_session_browser_tabs_for_tests,
    reset_tracked_session_browser_tabs_for_tests,
    track_session_browser_tab,
    untrack_session_browser_tab,
)

# Server-side error class re-exported here for client callers that want
# to catch a single name. Keep the import path stable.
from .._utils.errors import BrowserServiceError  # noqa: E402

__all__ = [
    "BrowserActions",
    "BrowserAuth",
    "BrowserServiceError",
    "Transport",
    "TrackedTab",
    "apply_proxy_paths",
    "close_tracked_browser_tabs_for_sessions",
    "count_tracked_session_browser_tabs_for_tests",
    "fetch_browser_json",
    "inject_auth_headers",
    "is_loopback_host",
    "is_loopback_url",
    "normalize_form_field",
    "persist_proxy_files",
    "reset_tracked_session_browser_tabs_for_tests",
    "set_default_dispatcher_app",
    "track_session_browser_tab",
    "untrack_session_browser_tab",
]


# Re-export BrowserAuth from the server module so client callers don't have
# to reach into ``server/auth.py`` directly. Kept at the bottom to avoid a
# circular import at module init time.
from ..server.auth import BrowserAuth  # noqa: E402
