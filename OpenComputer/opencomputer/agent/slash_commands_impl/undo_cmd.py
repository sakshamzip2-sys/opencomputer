"""``/undo`` — remove the last user/assistant exchange from the session.

Hermes-parity ("Remove the last user/assistant exchange"). A
*conversation-history* operation — distinct from ``/rollback``, which
restores filesystem checkpoints.

``undo_last_exchange`` truncates the session at the last
``role == "user"`` message, so a whole exchange — the user prompt, the
assistant reply, and any intervening tool messages — is removed
atomically and no ``tool_use``/``tool_result`` pair is orphaned.

The logic lives in the plain sync ``undo_last_exchange`` so both callers
use it without an event loop:

* ``UndoCommand`` — the agent ``SlashCommand`` (gateway / wire / ACP),
  reading ``runtime.custom['session_id']`` + ``['session_db']``.
* the ``oc chat`` cli_ui bridge (``_handle_undo`` -> ``SlashContext.on_undo``
  -> ``cli._on_undo``), which calls ``undo_last_exchange`` directly.
"""

from __future__ import annotations

import logging
from typing import Any

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

logger = logging.getLogger(__name__)


def _last_user_index(messages: list[Any]) -> int | None:
    """Index of the last ``role == "user"`` message, or None if there is none."""
    for i in range(len(messages) - 1, -1, -1):
        if getattr(messages[i], "role", None) == "user":
            return i
    return None


def undo_last_exchange(session_id: str, db: Any) -> str:
    """Remove the last user/assistant exchange from ``session_id``.

    Truncates the session at the last ``role == "user"`` message via
    ``SessionDB.replace_session_messages`` so the prompt, the reply, and
    any tool messages of that turn are removed as one unit. Never raises
    — failures are reported in the returned status string.
    """
    try:
        messages = list(db.get_messages(session_id))
    except Exception as e:  # noqa: BLE001 — surfaced in the return string
        return f"Failed to read messages: {type(e).__name__}: {e}"

    last_user = _last_user_index(messages)
    if last_user is None:
        return (
            "Nothing to undo — no user/assistant exchange in this "
            "session yet."
        )

    kept = messages[:last_user]
    removed = len(messages) - len(kept)
    try:
        db.replace_session_messages(session_id, kept)
    except Exception as e:  # noqa: BLE001 — surfaced in the return string
        return f"Failed to undo: {type(e).__name__}: {e}"

    logger.info("session %s: /undo removed %d message(s)", session_id, removed)
    plural = "" if removed == 1 else "s"
    return f"↩ Removed the last exchange ({removed} message{plural})."


class UndoCommand(SlashCommand):
    """``/undo`` — remove the last user/assistant exchange (Hermes-parity)."""

    name = "undo"
    description = "Remove the last user/assistant exchange from this session"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        sid = runtime.custom.get("session_id")
        db = runtime.custom.get("session_db")
        if not sid or db is None:
            return SlashCommandResult(
                output=(
                    "No active session — /undo only works inside an "
                    "agent loop turn."
                ),
                handled=True,
            )
        return SlashCommandResult(
            output=undo_last_exchange(sid, db), handled=True
        )


__all__ = ["UndoCommand", "undo_last_exchange"]
