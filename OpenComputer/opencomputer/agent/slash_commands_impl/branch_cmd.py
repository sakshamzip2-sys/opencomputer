"""``/branch [name]`` — fork the current conversation into a new session.

Tier 2.A.1 (Tier 2.A.1 originally was /copy; /branch is .12 by audit
ordering) from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

Mirrors the existing ``oc session fork`` CLI primitive, but as an
in-loop slash command so the user can branch mid-conversation without
leaving chat.

Usage:
    /branch                  → fork with default title "(fork)"
    /branch try-different    → fork with title "try-different"

The forked session is independent from this point — the user resumes
it later with ``oc chat --resume <id>``. The current session continues
unchanged.
"""

from __future__ import annotations

import uuid

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class BranchCommand(SlashCommand):
    name = "branch"
    description = "Fork the current conversation into a new session"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sid = runtime.custom.get("session_id")
        db = runtime.custom.get("session_db")
        if not sid or db is None:
            return SlashCommandResult(
                output="No active session — /branch only works inside an agent loop turn.",
                handled=True,
            )

        title = (args or "").strip()
        if len(title) > 200:
            return SlashCommandResult(
                output=f"title too long ({len(title)} chars); cap is 200",
                handled=True,
            )

        try:
            src = db.get_session(sid)
        except Exception as e:  # noqa: BLE001
            return SlashCommandResult(
                output=f"Failed to read source session: {type(e).__name__}: {e}",
                handled=True,
            )
        if src is None:
            return SlashCommandResult(
                output=f"Source session {sid!r} not found in DB.",
                handled=True,
            )

        try:
            messages = db.get_messages(sid)
        except Exception as e:  # noqa: BLE001
            return SlashCommandResult(
                output=f"Failed to read messages: {type(e).__name__}: {e}",
                handled=True,
            )

        new_id = uuid.uuid4().hex
        # Title: explicit > "<src title> (fork)" > "(fork)"
        if title:
            new_title = title
        else:
            src_title = (src.get("title") or "").strip()
            new_title = f"{src_title} (fork)".strip() if src_title else "(fork)"

        try:
            db.create_session(
                new_id,
                platform=src.get("platform", "") or "cli",
                model=src.get("model", "") or "",
                title=new_title,
            )
            if messages:
                db.append_messages_batch(new_id, messages)
        except Exception as e:  # noqa: BLE001
            return SlashCommandResult(
                output=f"Failed to create forked session: {type(e).__name__}: {e}",
                handled=True,
            )

        return SlashCommandResult(
            output=(
                f"Branched: new session [{new_id}] "
                f"with {len(messages)} message(s) copied.\n"
                f"Title: {new_title}\n"
                f"Resume with: oc chat --resume {new_id}"
            ),
            handled=True,
        )


__all__ = ["BranchCommand"]
