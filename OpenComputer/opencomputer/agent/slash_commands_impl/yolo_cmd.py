"""``/yolo [on|off|status]`` — toggle approval-gate bypass for the session.

Tier 2.A.19 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

YOLO mode skips the F1 ConsentGate prompt for the rest of the session.
The state lives in ``runtime.custom["yolo_session"]`` so other components
(consent gate, audit log) can inspect it. The audit log records the
elevation event so the change is never silent.

Subcommands:
    /yolo            → toggle current state
    /yolo on         → enable
    /yolo off        → disable
    /yolo status     → report current state without changing it

Always interactive: the user must explicitly type ``/yolo on`` to enable
(or ``/yolo`` while disabled). The ON message includes a clear warning so
the user is aware destructive tools will run unprompted.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_ON_MESSAGE = (
    "⚠ YOLO mode is now ON for this session.\n"
    "ConsentGate prompts will be skipped — destructive tools (Bash, Edit, "
    "Write, MultiEdit, network sends) run without confirmation.\n"
    "Type /yolo off to restore approval prompts."
)
_OFF_MESSAGE = "YOLO mode is now OFF. ConsentGate prompts restored."
_USAGE = (
    "Usage: /yolo [on|off|status]\n"
    "Skip the ConsentGate approval prompt for the rest of the session.\n"
    "WARNING: enabling means destructive tools run without confirmation."
)


class YoloCommand(SlashCommand):
    name = "yolo"
    description = "Toggle session-wide approval-gate bypass (skip ConsentGate)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = bool(runtime.custom.get("yolo_session", False))

        if sub == "":
            new_state = not current
        elif sub == "on":
            new_state = True
        elif sub == "off":
            new_state = False
        elif sub == "status":
            return SlashCommandResult(
                output=f"YOLO mode is currently {'ON' if current else 'OFF'}",
                handled=True,
            )
        else:
            return SlashCommandResult(output=_USAGE, handled=True)

        runtime.custom["yolo_session"] = new_state
        msg = _ON_MESSAGE if new_state else _OFF_MESSAGE
        return SlashCommandResult(output=msg, handled=True)


__all__ = ["YoloCommand"]
