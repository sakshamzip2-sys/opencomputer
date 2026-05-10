"""``/restore <id|label>`` — rewind this session's messages to a prompt checkpoint.

CC §11 from ``docs/OC-FROM-CLAUDE-CODE.md``. Companion to
``/checkpoint``. Backed by
``SessionDB.replace_session_messages_from_checkpoint`` — atomic
truncate + replay within one transaction.

Semantics:
  - The argument can be a checkpoint id (uuid4) OR a label. When
    multiple checkpoints share a label, the most-recently-created
    matching one wins.
  - The restore happens against the DB. The current turn's in-memory
    message list is NOT rolled back live; the user's next prompt
    starts a fresh ``run_conversation`` that reads from DB and sees
    the truncated history. The output text says so explicitly.
  - Refuses to restore across sessions — a checkpoint id from another
    session returns an error rather than corrupting the target.

``/restore`` with no argument prints the most recent 10 checkpoints
so users don't have to leave the chat to discover the id.
"""

from __future__ import annotations

from datetime import UTC, datetime

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


def _format_ts(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch, tz=UTC).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (OSError, OverflowError, ValueError):
        return "(invalid date)"


def _format_listing(checkpoints) -> str:
    """Render a tidy list. Used both for the no-arg path and when the
    argument doesn't resolve."""
    if not checkpoints:
        return "no checkpoints saved in this session yet — try /checkpoint first."
    lines = ["## Checkpoints (most recent first)"]
    for cp in checkpoints:
        sid_short = cp.id[:8]
        lines.append(
            f"  {sid_short}…  {_format_ts(cp.created_at)}  "
            f"prompt#{cp.prompt_index}  msgs={len(cp.messages)}  label={cp.label!r}"
        )
    lines.append("\nrestore with: /restore <id-prefix>  or  /restore <label>")
    return "\n".join(lines)


class RestoreCommand(SlashCommand):
    name = "restore"
    description = "Rewind this session's message history to a /checkpoint snapshot"

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
                    "no active session id available — /restore needs the agent "
                    "loop to plumb session_db + session_id into the runtime."
                ),
                handled=True,
            )

        arg = (args or "").strip()

        # No argument: list available checkpoints so the user can pick.
        if not arg:
            checkpoints = db.list_prompt_checkpoints(session_id, limit=10)
            return SlashCommandResult(
                output=_format_listing(checkpoints), handled=True
            )

        # Resolve argument: first try as a full id, then as label, then
        # as a unique id-prefix (against the 10 most-recent).
        cp = db.get_prompt_checkpoint(arg)
        if cp is None:
            cp = db.find_prompt_checkpoint_by_label(session_id=session_id, label=arg)
        if cp is None:
            # Prefix match — useful for /restore <8-char-id-prefix>.
            recent = db.list_prompt_checkpoints(session_id, limit=50)
            matching = [c for c in recent if c.id.startswith(arg)]
            if len(matching) == 1:
                cp = matching[0]
            elif len(matching) > 1:
                return SlashCommandResult(
                    output=(
                        f"prefix {arg!r} matches {len(matching)} checkpoints — "
                        "be more specific. Run /restore (no args) to list them."
                    ),
                    handled=True,
                )

        if cp is None:
            return SlashCommandResult(
                output=(
                    f"no checkpoint matches {arg!r} (tried id, label, prefix). "
                    "Run /restore (no args) to see available checkpoints."
                ),
                handled=True,
            )

        # Refuse cross-session restore. The SessionDB helper already
        # enforces this, but check here so the error message is clearer
        # than "0 rows inserted".
        if cp.session_id != session_id:
            return SlashCommandResult(
                output=(
                    f"checkpoint {cp.id} belongs to a different session "
                    f"({cp.session_id[:8]}…). Cross-session restore is not "
                    "supported — resume that session first."
                ),
                handled=True,
            )

        try:
            inserted = db.replace_session_messages_from_checkpoint(
                session_id=session_id, checkpoint_id=cp.id
            )
        except Exception as exc:  # noqa: BLE001 — slash must never crash
            return SlashCommandResult(
                output=f"/restore failed: {type(exc).__name__}: {exc}",
                handled=True,
            )

        if inserted == 0:
            return SlashCommandResult(
                output=(
                    f"/restore made no changes — the snapshot for {cp.id} appears "
                    "empty. Use /restore (no args) to find a different snapshot."
                ),
                handled=True,
            )

        return SlashCommandResult(
            output=(
                f"restored session to checkpoint {cp.id[:8]}… "
                f"(label={cp.label!r}, {inserted} messages)\n"
                "Your next prompt will continue from this point. The current "
                "turn's in-memory state is unchanged until you send a new message."
            ),
            handled=True,
        )


__all__ = ["RestoreCommand"]
