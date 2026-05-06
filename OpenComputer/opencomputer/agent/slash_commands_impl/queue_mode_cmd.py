"""``/queue-mode [followup|interrupt|status]`` — set or inspect the inbound queue mode.

Phase 2 (S1 from 2026-05-06 OpenClaw deep-comparison brief). Bridges
the slash-command surface to ``QueueManager.set_session_mode`` via the
gateway singleton accessor.
"""

from __future__ import annotations

from plugin_sdk.queue import ALL_QUEUE_MODES, QueueMode
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class QueueModeCommand(SlashCommand):
    name = "queue-mode"
    description = "Set inbound message queue mode (followup|interrupt|status)"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        manager = _get_queue_manager()
        session_id = (
            runtime.custom.get("session_id")
            if runtime is not None and runtime.custom is not None
            else None
        )

        if sub == "" or sub == "status":
            current = (
                manager.get_session_mode(session_id)
                if (manager is not None and session_id is not None)
                else None
            )
            default = manager.default_mode if manager is not None else "followup"
            if current is None:
                return SlashCommandResult(
                    output=(
                        f"queue mode: {default} (default; no session override active)\n"
                        f"available: {', '.join(ALL_QUEUE_MODES)}"
                    ),
                    handled=True,
                )
            return SlashCommandResult(
                output=(
                    f"queue mode: {current} "
                    f"(session override; default={default})\n"
                    f"available: {', '.join(ALL_QUEUE_MODES)}"
                ),
                handled=True,
            )

        if sub not in ALL_QUEUE_MODES:
            return SlashCommandResult(
                output=(
                    f"unknown queue mode {sub!r}; "
                    f"available: {', '.join(ALL_QUEUE_MODES)}"
                ),
                handled=True,
            )

        if manager is None or session_id is None:
            return SlashCommandResult(
                output=(
                    "queue manager not reachable — set OPENCOMPUTER_GATEWAY=1 "
                    "or run inside `opencomputer gateway` for /queue-mode to take effect."
                ),
                handled=True,
            )

        manager.set_session_mode(session_id, sub)  # type: ignore[arg-type]
        # Validate cast (mypy will warn but the membership check above is the guard).
        new_mode: QueueMode = sub  # type: ignore[assignment]
        return SlashCommandResult(
            output=f"queue mode set to: {new_mode}",
            handled=True,
        )


def _get_queue_manager():
    """Return the active QueueManager registered by Dispatch, or None.

    Imports lazily so plugin_sdk-only environments (no gateway present)
    don't pay the gateway-import cost.
    """
    try:
        from opencomputer.gateway.queue_manager import get_active_manager

        return get_active_manager()
    except Exception:  # noqa: BLE001 — gateway optional
        return None


__all__ = ["QueueModeCommand"]
