"""Chrome process management for the OpenClaw browser port (W0c).

Modules:
  executables      cross-platform binary detection (macOS plist / Linux xdg /
                    Windows registry + hardcoded fallbacks)
  launch           spawn-and-wait — bootstrap user-data-dir, decorate, real launch
  lifecycle        is_chrome_reachable / is_chrome_cdp_ready / stop_openclaw_chrome
  decoration       atomic mutation of Local State + Default/Preferences JSON

Depends on profiles/ (resolved config types) and _utils/ (atomic write).
"""

from __future__ import annotations

from .decoration import (
    decorate_openclaw_profile,
    ensure_profile_clean_exit,
    is_profile_decorated,
    parse_hex_rgb_to_signed_argb_int,
)
from .executables import (
    parse_browser_major_version,
    read_browser_version,
    resolve_chrome_executable,
)
from .launch import (
    RunningChrome,
    build_chrome_launch_args,
    launch_openclaw_chrome,
    resolve_openclaw_user_data_dir,
)
from .lifecycle import (
    is_chrome_cdp_ready,
    is_chrome_reachable,
    stop_openclaw_chrome,
)

__all__ = [
    "RunningChrome",
    "build_chrome_launch_args",
    "decorate_openclaw_profile",
    "ensure_profile_clean_exit",
    "is_chrome_cdp_ready",
    "is_chrome_reachable",
    "is_profile_decorated",
    "launch_openclaw_chrome",
    "parse_browser_major_version",
    "parse_hex_rgb_to_signed_argb_int",
    "read_browser_version",
    "resolve_chrome_executable",
    "resolve_openclaw_user_data_dir",
    "stop_openclaw_chrome",
]
