"""Server-context — orchestrator state holder + per-profile lifecycle.

Public surface:
  - BrowserServerState, ProfileRuntimeState, ReconcileMarker
  - TabInfo dataclass
  - ensure_profile_running, teardown_profile
  - select_target_id (last_target_id fallback chain)
  - open_tab, focus_tab, close_tab

Depends on chrome/ (RunningChrome, launch_openclaw_chrome, stop_openclaw_chrome),
profiles/ (ResolvedBrowserProfile + capabilities), and snapshot/ (Chrome MCP)
for the capability-routed paths. The exact dispatch is done via injected
"driver" callables so tests don't need a real Chrome / npx.
"""

from __future__ import annotations

from .lifecycle import (
    ProfileDriver,
    ensure_profile_running,
    teardown_profile,
)
from .selection import (
    AmbiguousTargetIdError,
    TabNotFoundError,
    resolve_target_id_from_tabs,
    select_target_id,
)
from .state import (
    BrowserServerState,
    ProfileRuntimeState,
    ProfileStatus,
    ReconcileMarker,
    TabInfo,
)
from .tab_ops import (
    close_tab,
    focus_tab,
    open_tab,
)

__all__ = [
    "AmbiguousTargetIdError",
    "BrowserServerState",
    "ProfileDriver",
    "ProfileRuntimeState",
    "ProfileStatus",
    "ReconcileMarker",
    "TabInfo",
    "TabNotFoundError",
    "close_tab",
    "ensure_profile_running",
    "focus_tab",
    "open_tab",
    "resolve_target_id_from_tabs",
    "select_target_id",
    "teardown_profile",
]
