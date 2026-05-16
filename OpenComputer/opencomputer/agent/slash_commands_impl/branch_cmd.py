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

Implementation note: shares
:func:`opencomputer.agent.session_fork.fork_session` with the CLI
``oc session fork``. The slash opts in to ``record_parent=True`` so
the resume picker can group the fork under its source (Phase H,
2026-05-11). The CLI keeps the pre-Phase-H behaviour.
"""

from __future__ import annotations

from opencomputer.agent.session_fork import (
    TITLE_MAX_LEN,
    SourceSessionNotFoundError,
    fork_session,
)
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
        # Reject over-length titles loudly (the helper silently truncates,
        # which is fine for the CLI but the slash wants to be explicit so
        # the user knows their input was rejected, not mangled).
        if len(title) > TITLE_MAX_LEN:
            return SlashCommandResult(
                output=f"title too long ({len(title)} chars); cap is {TITLE_MAX_LEN}",
                handled=True,
            )

        try:
            result = fork_session(
                db,
                sid,
                title=title or None,
                record_parent=True,
            )
        except SourceSessionNotFoundError:
            return SlashCommandResult(
                output=f"Source session {sid!r} not found in DB.",
                handled=True,
            )
        except Exception as e:  # noqa: BLE001 — slash must never crash the chat
            return SlashCommandResult(
                output=f"Failed to create forked session: {type(e).__name__}: {e}",
                handled=True,
            )

        # 2026-05-11 — PI-style summary card. Pure-text Unicode box so
        # we don't need a Rich Console here; the chat output stream
        # renders it verbatim and the user sees a clear "branch event"
        # marker rather than three terse lines that blend into the
        # rest of the assistant text.
        from opencomputer.cli_ui.summary_cards import render_branch_card

        return SlashCommandResult(
            output=render_branch_card(
                new_session_id=result.new_session_id,
                title=result.new_title,
                messages_copied=result.messages_copied,
            ),
            handled=True,
        )


__all__ = ["BranchCommand"]
