"""``/status`` — show session info (platform, chat, session id, model, queue).

Read-only summary — pulls everything from ``runtime.custom`` plus the
gateway's ``QueueManager`` singleton (when one is registered, e.g. inside
``oc gateway``). Outside the gateway the queue line shows the default
mode and notes that no session override is active.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class StatusCommand(SlashCommand):
    name = "status"
    description = "Show session info (platform, chat, model, queue mode, last activity)"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        custom = runtime.custom or {}

        platform = custom.get("platform") or "(none)"
        chat_id = custom.get("chat_id") or "(none)"
        session_id = custom.get("session_id") or "(none)"
        model = custom.get("model") or "(unknown)"
        last_activity = custom.get("last_activity") or "(never)"

        # Queue mode — best effort (gateway registers a manager via
        # set_active_manager; outside the gateway this returns None).
        manager, queue_mode = _queue_summary(session_id)

        lines = [
            "## Session status",
            f"  platform:      {platform}",
            f"  chat_id:       {chat_id}",
            f"  session_id:    {session_id}",
            f"  model:         {model}",
            f"  queue_mode:    {queue_mode}",
            f"  last_activity: {last_activity}",
        ]
        if manager is None:
            lines.append("  (queue manager not active — running outside gateway)")
        return SlashCommandResult(output="\n".join(lines), handled=True)


def _queue_summary(session_id: str | None) -> tuple[object | None, str]:
    """Return (manager, queue_mode_label).

    Lazy import keeps the slash-command surface usable in plugin_sdk-only
    test contexts that don't depend on the gateway package.
    """
    try:
        from opencomputer.gateway.queue_manager import get_active_manager

        manager = get_active_manager()
    except Exception:  # noqa: BLE001 — gateway optional
        return None, "(unknown)"

    if manager is None:
        return None, "(unknown)"

    default = getattr(manager, "default_mode", "followup")
    if session_id and session_id != "(none)":
        try:
            override = manager.get_session_mode(session_id)
        except Exception:  # noqa: BLE001 — defensive
            override = None
        if override is not None:
            return manager, f"{override} (session override; default={default})"
    return manager, f"{default} (default)"


__all__ = ["StatusCommand"]
