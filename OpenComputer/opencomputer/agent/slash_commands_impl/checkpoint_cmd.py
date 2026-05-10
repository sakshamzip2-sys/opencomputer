"""``/checkpoint [label]`` — save a restorable snapshot of this session.

CC §11 from ``docs/OC-FROM-CLAUDE-CODE.md``. Backed by the
``prompt_checkpoints`` table (schema v15, 2026-05-09).

This snapshots the in-memory message list at the moment of the slash
dispatch. The companion ``/restore <id>`` rewinds the session's DB
messages to that snapshot — the user's next prompt will continue from
the restored point.

Distinct from ``/rollback`` which operates on the RewindStore (a
filesystem-level checkpoint backing the ``rewind`` Python package, not
the prompt history).

Reads from ``runtime.custom``:

  - ``session_id``           — set by the agent loop each turn
  - ``session_db``           — :class:`SessionDB` reference (loop plumbs
                               this for session-aware slash commands;
                               same plumbing as ``/title``, ``/history``)
  - ``current_messages``     — optional list of in-flight messages
                               (preferred); when absent the slash
                               reads the persisted DB rows instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


def _coerce_label(arg: str) -> str:
    """Strip + normalise the label. Empty input → auto-label with
    the local-time short timestamp. We never write an empty label."""
    cleaned = (arg or "").strip()
    if cleaned:
        return cleaned[:80]  # cap; users rarely benefit from longer
    return "auto-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _snapshot_messages(runtime: RuntimeContext, db, session_id: str) -> list[dict[str, Any]]:
    """Prefer the loop's in-flight ``current_messages`` (most accurate
    for "where we are right now"). Fall back to DB rows when absent —
    e.g. older loop builds that don't plumb the live list."""
    live = runtime.custom.get("current_messages") if runtime is not None else None
    if isinstance(live, list) and live:
        return [_msg_to_dict(m) for m in live]
    if db is None or not session_id:
        return []
    try:
        rows = db.get_messages(session_id)
    except Exception:  # noqa: BLE001 — slash must never crash
        return []
    return [_msg_to_dict(m) for m in rows]


def _msg_to_dict(m: Any) -> dict[str, Any]:
    """Normalise either a :class:`plugin_sdk.core.Message` dataclass
    or a plain dict into the dict shape we persist in
    ``prompt_checkpoints.messages_snapshot_json``."""
    if hasattr(m, "__dict__"):
        d = {k: v for k, v in m.__dict__.items() if not k.startswith("_")}
    elif isinstance(m, dict):
        d = dict(m)
    else:
        return {"role": "user", "content": str(m)}
    # Tool calls might be Message dataclasses — recurse once.
    if d.get("tool_calls"):
        d["tool_calls"] = [
            tc.__dict__ if hasattr(tc, "__dict__") else tc
            for tc in d["tool_calls"]
        ]
    return d


class CheckpointCommand(SlashCommand):
    name = "checkpoint"
    description = "Save a named restorable snapshot of this session's message history"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        session_id = (
            (runtime.custom.get("session_id") or "")
            if runtime is not None
            else ""
        )
        db = runtime.custom.get("session_db") if runtime is not None else None
        if not session_id or db is None:
            return SlashCommandResult(
                output=(
                    "no active session id available — /checkpoint needs the "
                    "agent loop to plumb session_db + session_id into the runtime. "
                    "Try /checkpoint after the first turn completes."
                ),
                handled=True,
            )

        messages = _snapshot_messages(runtime, db, session_id)
        if not messages:
            return SlashCommandResult(
                output=(
                    "no messages in this session yet — send at least one prompt "
                    "before checkpointing."
                ),
                handled=True,
            )

        label = _coerce_label(args)
        # Optional prompt_index — caller's turn counter when available.
        prompt_index = int(runtime.custom.get("turn_index") or 0)

        try:
            cp_id = db.create_prompt_checkpoint(
                session_id=session_id,
                prompt_index=prompt_index,
                messages=messages,
                label=label,
            )
        except Exception as exc:  # noqa: BLE001 — slash must never crash
            return SlashCommandResult(
                output=f"/checkpoint failed: {type(exc).__name__}: {exc}",
                handled=True,
            )

        return SlashCommandResult(
            output=(
                f"checkpoint saved: id={cp_id} label={label!r}\n"
                f"  prompt_index={prompt_index}  messages={len(messages)}\n"
                f"  restore later with: /restore {cp_id}  (or /restore {label})"
            ),
            handled=True,
        )


__all__ = ["CheckpointCommand"]
