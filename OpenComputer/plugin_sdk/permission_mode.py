"""PermissionMode enum + effective_permission_mode() resolver.

Single source of truth for "what mode is this session in right now?"
Resolution precedence (top wins):

  1. runtime.custom["permission_mode"]            (canonical session-mutable)
  2. runtime.custom["plan_mode"] == True          → PLAN  (legacy /plan)
     runtime.custom["yolo_session"] == True       → AUTO  (legacy /yolo)
  3. runtime.permission_mode                      (canonical CLI-set field)
  4. runtime.plan_mode == True                    → PLAN  (legacy --plan)
     runtime.yolo_mode == True                    → AUTO  (legacy --yolo)
  5. PermissionMode.DEFAULT

Plan beats auto on conflict (matches existing CLI precedence at
``cli.py:879``). New code should call this helper rather than reading
any individual field directly.
"""
from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugin_sdk.runtime_context import RuntimeContext


class PermissionMode(StrEnum):
    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "accept-edits"
    AUTO = "auto"


def effective_permission_mode(runtime: "RuntimeContext") -> PermissionMode:
    # 1. Canonical session-mutable key.
    custom_mode = runtime.custom.get("permission_mode")
    if custom_mode:
        try:
            return PermissionMode(custom_mode)
        except ValueError:
            pass  # malformed value — fall through to next precedence layer

    # 2. Legacy session-mutable keys (plan beats auto on conflict).
    if runtime.custom.get("plan_mode"):
        return PermissionMode.PLAN
    if runtime.custom.get("yolo_session"):
        return PermissionMode.AUTO

    # 3. Canonical CLI-set frozen field.
    if runtime.permission_mode != PermissionMode.DEFAULT:
        return runtime.permission_mode

    # 4. Legacy CLI-set fields (plan beats yolo).
    if runtime.plan_mode:
        return PermissionMode.PLAN
    if runtime.yolo_mode:
        return PermissionMode.AUTO

    # 5. Default.
    return PermissionMode.DEFAULT


__all__ = ["PermissionMode", "effective_permission_mode"]
