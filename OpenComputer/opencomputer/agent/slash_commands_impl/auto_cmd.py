"""``/auto [on|off|status]`` — toggle auto mode (skip per-action confirmations).

Renamed from ``/yolo``. ``YoloCommand`` is a deprecated alias kept for one
minor version; it forwards to ``AutoCommand`` and prints a one-shot
deprecation line.

State is stored in two ``runtime.custom`` keys for backwards compatibility:

  * ``runtime.custom["permission_mode"] = "auto"``  — canonical
    (read by :func:`plugin_sdk.effective_permission_mode`)
  * ``runtime.custom["yolo_session"] = True``       — legacy
    (still read by older readers + audit-log consumers)

Auto mode skips the F1 ConsentGate for the rest of the session — destructive
tools (Bash, Edit, Write, MultiEdit, network sends) run without confirmation.
The user must explicitly opt in (``/auto`` defaults to *toggle*; ``on`` always
enables; ``off`` always disables).
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_ON_MESSAGE = (
    "⚠ Auto mode is now ON for this session.\n"
    "Per-action ConsentGate prompts will be skipped — destructive tools "
    "(Bash, Edit, Write, MultiEdit, network sends) run without confirmation.\n"
    "Type /auto off to restore approval prompts."
)
_OFF_MESSAGE = "Auto mode is now OFF. ConsentGate prompts restored."
_USAGE = (
    "Usage: /auto [on|off|status]\n"
    "Skip per-action confirmation prompts for the rest of the session.\n"
    "WARNING: enabling means destructive tools run without confirmation."
)


class AutoCommand(SlashCommand):
    name = "auto"
    description = "Toggle auto mode (skip per-action confirmation prompts)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = runtime.custom.get("permission_mode") == "auto" or runtime.custom.get(
            "yolo_session", False
        )

        if sub == "":
            new_state = not current
        elif sub == "on":
            new_state = True
        elif sub == "off":
            new_state = False
        elif sub == "status":
            return SlashCommandResult(
                output=f"Auto mode is currently {'ON' if current else 'OFF'}",
                handled=True,
            )
        else:
            return SlashCommandResult(output=_USAGE, handled=True)

        if new_state:
            runtime.custom["permission_mode"] = "auto"
            runtime.custom["yolo_session"] = True  # legacy compat
            # M9.3: explicit user re-arm clears any prior block budget.
            # If the M9.2 classifier had paused auto mode after 3
            # consecutive / 20 total blocks, `/auto on` is the canonical
            # resume signal — the user has acknowledged the pause.
            paused_session = runtime.custom.pop("m9_3_paused_session", None)
            if paused_session:
                try:
                    from opencomputer.agent.tool_call_classifier import (
                        reset_block_budget as _m93_reset,
                    )
                    _m93_reset(paused_session)
                except Exception:  # noqa: BLE001 — never let reset failure block /auto
                    pass
        else:
            # Only clear the canonical key if it was set to AUTO; users may
            # have toggled to a different mode and we don't want /auto off to
            # also disable, e.g., plan mode.
            if runtime.custom.get("permission_mode") == "auto":
                runtime.custom.pop("permission_mode", None)
            runtime.custom.pop("yolo_session", None)
            runtime.custom.pop("m9_3_paused_session", None)

        msg = _ON_MESSAGE if new_state else _OFF_MESSAGE
        return SlashCommandResult(output=msg, handled=True)


class YoloCommand(SlashCommand):
    name = "yolo"
    description = "[deprecated] Alias for /auto"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        # Local import to avoid a circular at module load (cli imports
        # slash_commands eagerly during boot).
        from opencomputer.cli import _emit_yolo_deprecation
        _emit_yolo_deprecation()
        result = await AutoCommand().execute(args, runtime)
        return SlashCommandResult(
            output=f"[deprecated — use /auto] {result.output}",
            handled=True,
        )


__all__ = ["AutoCommand", "YoloCommand"]
