"""Emergency consent bypass + AUTO-mode bypass.

Two activation paths:

1. ``OPENCOMPUTER_CONSENT_BYPASS=1`` env var — process-wide emergency unbrick
   when the gate misbehaves. Every action audit-logged under actor="bypass".

2. ``effective_permission_mode(runtime) == AUTO`` — explicit user opt-in via
   ``--auto`` (or legacy ``--yolo`` / ``/yolo on``). Same audit treatment as
   the env-var bypass — the user has chosen to skip per-call prompts and the
   audit log is the accountability layer.

Either path triggers the bypass banner.

Intentionally env-only on the first path: a one-off flag is easy to lose
track of, while setting an env var forces a deliberate action and keeps
the "active" state visible via ``env | grep BYPASS``.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugin_sdk.runtime_context import RuntimeContext


class BypassManager:
    ENV_FLAG = "OPENCOMPUTER_CONSENT_BYPASS"

    @classmethod
    def is_active(cls, runtime: "RuntimeContext | None" = None) -> bool:
        if cls._env_active():
            return True
        if runtime is not None and cls._auto_mode_active(runtime):
            return True
        return False

    @classmethod
    def _env_active(cls) -> bool:
        return os.environ.get(cls.ENV_FLAG, "").strip().lower() in (
            "1", "true", "yes", "on",
        )

    @staticmethod
    def _auto_mode_active(runtime: "RuntimeContext") -> bool:
        # Local import to keep plugin_sdk dependency direction (sdk → no opencomputer).
        from plugin_sdk.permission_mode import (
            PermissionMode,
            effective_permission_mode,
        )
        return effective_permission_mode(runtime) == PermissionMode.AUTO

    @staticmethod
    def banner() -> str:
        return (
            "⚠️ CONSENT BYPASS ACTIVE — every tool call will run without gate.\n"
            "Every action is being heavily audit-logged. Disable to restore "
            "normal operation (unset OPENCOMPUTER_CONSENT_BYPASS or switch "
            "out of auto mode)."
        )
