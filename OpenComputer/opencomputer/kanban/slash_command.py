"""``/kanban`` slash command for in-chat kanban control (Wave 6.E.6).

Hermes exposes ``/kanban`` from gateway chats so users can read +
write the board mid-conversation without leaving the UI. We mirror
that here.

Design points:

- The slash command runs the same ``oc kanban`` argparse path the CLI
  uses, captures stdout via :class:`io.StringIO`, and returns the text
  truncated to platform-friendly length.
- ``bypass_running_guard = True`` — a board-read or comment-add must
  reach the database even when the agent is mid-turn. Kanban DB uses
  WAL + ``BEGIN IMMEDIATE`` so concurrent writers serialize cleanly.
- On ``/kanban create`` from a gateway chat, auto-subscribe the
  originating chat to terminal events (Hermes parity). Idempotent
  via ``add_notify_sub``'s ``INSERT OR IGNORE``.

Registration: module-level :func:`register_kanban_slash_commands` is
called by the gateway boot path so the command is available the
moment the gateway is up. Kept out of plugin land because kanban
ships in-package, not as an ``extensions/*`` plugin.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import shlex
from contextlib import redirect_stdout
from typing import Any

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

logger = logging.getLogger("opencomputer.kanban.slash")

# Larger than the 3800 platform-cap because the CLI surface produces
# tables that are still useful when truncated. The gateway truncates
# again per-platform when delivering.
SLASH_OUTPUT_CAP = 6000


class KanbanSlashCommand(SlashCommand):
    """``/kanban <verb> [args...]`` — runs the same logic as ``oc kanban``."""

    name = "kanban"
    description = "Read + write the kanban board from chat"
    aliases: tuple[str, ...] = ()

    # Wave 6.E.6 — opt-in flag honored by the slash dispatcher when an
    # agent turn is in flight. Kanban operations are durable + cheap
    # and shouldn't be queued behind a long-running agent reply.
    bypass_running_guard: bool = True

    async def execute(
        self, args: str, runtime: RuntimeContext,
    ) -> SlashCommandResult:
        """Parse ``args`` as ``oc kanban`` argv and run the command.

        Output is captured + truncated. If the verb is ``create`` AND
        a gateway chat originated the request, the resulting task gets
        an auto-subscription for terminal-event notifications.
        """
        from opencomputer.gateway._truncate import truncate_smart
        from opencomputer.kanban import cli as kb_cli

        # Empty args → list verb, mirroring hermes.
        argv = shlex.split(args) if args.strip() else ["list"]
        verb = argv[0] if argv else "list"

        # Build a fresh argparse parser on every call. Cheap; avoids
        # shared-state leaks from earlier invocations.
        parser = argparse.ArgumentParser(prog="kanban", add_help=False)
        sub = parser.add_subparsers(dest="kanban_action")
        kb_cli.build_parser(sub)

        try:
            parsed = parser.parse_args(argv)
        except SystemExit:
            # argparse calls sys.exit on bad args. Catch + return a
            # graceful error string.
            return SlashCommandResult(
                output=f"/kanban: bad arguments — try `/kanban help`.\nGiven: `{args}`",
                handled=True,
            )
        except Exception as exc:  # noqa: BLE001
            return SlashCommandResult(
                output=f"/kanban: parse error — {exc}",
                handled=True,
            )

        # Capture stdout the CLI writes via print().
        buf = io.StringIO()
        rc = 0
        try:
            with redirect_stdout(buf):
                rc = kb_cli.kanban_command(parsed) or 0
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("/kanban execution raised")
            return SlashCommandResult(
                output=f"/kanban {verb}: error — {type(exc).__name__}: {exc}",
                handled=True,
            )

        output = buf.getvalue().rstrip()

        # Auto-subscribe on create from a gateway chat.
        if verb == "create" and rc == 0:
            try:
                self._maybe_auto_subscribe(parsed, output, runtime)
            except Exception:  # noqa: BLE001
                # Subscription is best-effort — never fail the create.
                logger.exception("/kanban auto-subscribe failed (ignored)")

        if rc != 0 and not output:
            output = f"/kanban {verb}: exited with code {rc}"

        return SlashCommandResult(
            output=truncate_smart(output, max_len=SLASH_OUTPUT_CAP) or "(no output)",
            handled=True,
        )

    def _maybe_auto_subscribe(
        self,
        parsed: argparse.Namespace,
        output: str,
        runtime: RuntimeContext,
    ) -> None:
        """If the originating context has a gateway chat, subscribe it."""
        platform = runtime.custom.get("platform")
        chat_id = runtime.custom.get("chat_id")
        if not platform or not chat_id:
            return
        # The CLI prints the new task id when ``--json`` was used; for
        # non-json output the id is parseable from the first line. For
        # robustness we also scan the output for an id-shaped token.
        task_id = self._parse_task_id(output, parsed)
        if not task_id:
            return
        from opencomputer.kanban import db as kdb

        with kdb.connect() as conn:
            kdb.add_notify_sub(
                conn,
                task_id=task_id,
                platform=str(platform),
                chat_id=str(chat_id),
                thread_id=runtime.custom.get("thread_id"),
                user_id=runtime.custom.get("user_id"),
            )

    @staticmethod
    def _parse_task_id(output: str, parsed: argparse.Namespace) -> str | None:
        """Extract the new task id from `kanban create`'s output.

        Two paths:
        1. JSON output — try to load and read ``id``.
        2. Human output — first line is `created: <id>` or similar;
           regex out an id-shaped token (alphanumerics + dash).
        """
        if not output:
            return None
        # JSON path
        try:
            data = json.loads(output)
            if isinstance(data, dict) and "id" in data:
                return str(data["id"])
        except (ValueError, TypeError):
            pass
        # Human path — kanban CLI prints "task <id> ..." on success
        import re

        m = re.search(r"\b(t[a-z0-9_-]{4,})\b", output)
        if m:
            return m.group(1)
        return None


def register_kanban_slash_commands(plugin_registry: Any) -> None:
    """Register :class:`KanbanSlashCommand` on the global plugin registry.

    Called from the gateway boot path. Idempotent — re-registering
    overrides the previous instance.
    """
    plugin_registry.slash_commands[KanbanSlashCommand.name] = KanbanSlashCommand()
    logger.info("registered /kanban slash command (bypass_running_guard=True)")


__all__ = ["KanbanSlashCommand", "register_kanban_slash_commands", "SLASH_OUTPUT_CAP"]
