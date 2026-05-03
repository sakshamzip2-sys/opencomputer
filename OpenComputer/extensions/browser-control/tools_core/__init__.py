"""Tools-core: per-act-kind dispatch + arming + storage + snapshot orchestration.

Wave W2a — the workhorse subsystem.

Public surface (re-exports from leaf modules):

  - ``execute_single_action`` and ``EvaluateDisabledError`` (interactions)
  - ``snapshot_role_via_playwright`` (snapshot orchestration)
  - ``ref_locator`` and ``UnknownRefError`` (refs)
  - ``arm_dialog`` (dialog)
  - ``arm_file_chooser`` (file_chooser)
  - ``arm_download`` / ``capture_download`` / ``await_and_save_download`` (downloads)
  - cookies + storage helpers (storage)
  - trace start/stop (trace)
  - emulation knobs (state)
  - response body reader (responses)
  - activity tracking (activity)
  - shared timing constants + ``assert_interaction_navigation_completed_safely``
    (shared)

Each submodule is independently testable. Tests live in
``OpenComputer/tests/test_browser_port_tools_core_*.py``.
"""

from __future__ import annotations

from .activity import (
    clear_activity,
    last_action_time,
    record_action,
    seconds_since_last_action,
)
from .dialog import arm_dialog
from .downloads import (
    DownloadHandle,
    DownloadResult,
    DownloadSupersededError,
    arm_download,
    await_and_save_download,
    capture_download,
)
from .file_chooser import arm_file_chooser
from .interactions import (
    EvaluateDisabledError,
    execute_single_action,
    is_act_kind,
    supported_act_kinds,
)
from .refs import UnknownRefError, ref_locator, store_refs_into_session
from .responses import read_response_body
from .shared import (
    ACT_DEFAULT_INTERACTION_TIMEOUT_MS,
    ACT_DEFAULT_WAIT_TIMEOUT_MS,
    ACT_MAX_BATCH_ACTIONS,
    ACT_MAX_BATCH_DEPTH,
    ACT_MAX_CLICK_DELAY_MS,
    ACT_MAX_INTERACTION_TIMEOUT_MS,
    ACT_MAX_WAIT_TIME_MS,
    ACT_MAX_WAIT_TIMEOUT_MS,
    ACT_MIN_TIMEOUT_MS,
    INTERACTION_NAVIGATION_GRACE_MS,
    assert_interaction_navigation_completed_safely,
    clamp_interaction_timeout,
    clamp_wait_timeout,
    normalize_timeout_ms,
    require_ref,
    require_ref_or_selector,
    to_ai_friendly_error,
)
from .snapshot import AriaModeUnsupportedError, snapshot_role_via_playwright
from .state import (
    emulate_color_scheme,
    emulate_device,
    set_extra_http_headers,
    set_geolocation,
    set_http_credentials,
    set_locale,
    set_offline,
    set_timezone,
)
from .storage import (
    add_cookie,
    clear_cookies,
    get_cookies,
    storage_clear,
    storage_get,
    storage_remove,
    storage_set,
)
from .trace import (
    TraceAlreadyRunningError,
    TraceNotRunningError,
    is_trace_active,
    start_trace,
    stop_trace,
)

__all__ = [
    "ACT_DEFAULT_INTERACTION_TIMEOUT_MS",
    "ACT_DEFAULT_WAIT_TIMEOUT_MS",
    "ACT_MAX_BATCH_ACTIONS",
    "ACT_MAX_BATCH_DEPTH",
    "ACT_MAX_CLICK_DELAY_MS",
    "ACT_MAX_INTERACTION_TIMEOUT_MS",
    "ACT_MAX_WAIT_TIME_MS",
    "ACT_MAX_WAIT_TIMEOUT_MS",
    "ACT_MIN_TIMEOUT_MS",
    "INTERACTION_NAVIGATION_GRACE_MS",
    "AriaModeUnsupportedError",
    "DownloadHandle",
    "DownloadResult",
    "DownloadSupersededError",
    "EvaluateDisabledError",
    "TraceAlreadyRunningError",
    "TraceNotRunningError",
    "UnknownRefError",
    "add_cookie",
    "arm_dialog",
    "arm_download",
    "arm_file_chooser",
    "assert_interaction_navigation_completed_safely",
    "await_and_save_download",
    "capture_download",
    "clamp_interaction_timeout",
    "clamp_wait_timeout",
    "clear_activity",
    "clear_cookies",
    "emulate_color_scheme",
    "emulate_device",
    "execute_single_action",
    "get_cookies",
    "is_act_kind",
    "is_trace_active",
    "last_action_time",
    "normalize_timeout_ms",
    "read_response_body",
    "record_action",
    "ref_locator",
    "require_ref",
    "require_ref_or_selector",
    "seconds_since_last_action",
    "set_extra_http_headers",
    "set_geolocation",
    "set_http_credentials",
    "set_locale",
    "set_offline",
    "set_timezone",
    "snapshot_role_via_playwright",
    "start_trace",
    "stop_trace",
    "storage_clear",
    "storage_get",
    "storage_remove",
    "storage_set",
    "store_refs_into_session",
    "supported_act_kinds",
    "to_ai_friendly_error",
]
