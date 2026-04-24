"""Emergency consent bypass — for unbricking when the gate misbehaves.

If a bug or misconfiguration makes the gate block everything, the user
needs a way to get back to a working agent. `OPENCOMPUTER_CONSENT_BYPASS=1`
in the environment disables the gate for the duration of that process,
with every action audit-logged under actor="bypass" and a banner rendered
on every prompt so the user cannot forget it is on.

Intentionally env-only (not a CLI flag): a one-off flag is easy to lose
track of, while setting an env var forces a deliberate action and keeps
the "active" state visible via `env | grep BYPASS`.
"""
from __future__ import annotations

import os


class BypassManager:
    ENV_FLAG = "OPENCOMPUTER_CONSENT_BYPASS"

    @classmethod
    def is_active(cls) -> bool:
        return os.environ.get(cls.ENV_FLAG, "").strip().lower() in (
            "1", "true", "yes", "on",
        )

    @staticmethod
    def banner() -> str:
        return (
            "⚠️ CONSENT BYPASS ACTIVE — every tool call will run without gate.\n"
            "Every action is being heavily audit-logged. Unset "
            "OPENCOMPUTER_CONSENT_BYPASS to restore normal operation."
        )
