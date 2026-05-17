"""Tests for the /undo slash command — Hermes-parity remove-last-exchange.

Hermes ships /undo ("Remove the last user/assistant exchange"). It is a
conversation-history operation, distinct from /rollback (which restores
filesystem checkpoints). /undo truncates the session at the last
role=="user" message so a whole exchange — the prompt, the reply, and
any intervening tool messages — is removed atomically and no
tool_use/tool_result pair is ever orphaned.

UndoCommand (an agent SlashCommand) carries the logic. It is reachable
on gateway/wire/ACP via _BUILTIN_COMMANDS, and in `oc chat` via a
cli_ui bridge (a CommandDef + _handle_undo -> SlashContext.on_undo),
mirroring how /reasoning and /sources bridge the two registries.

These tests use a real SessionDB (a temp SQLite file) so the
get_messages -> replace_session_messages round-trip is genuinely
exercised, not faked.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from rich.console import Console

from opencomputer.agent.slash_commands import get_registered_commands
from opencomputer.agent.slash_commands_impl.undo_cmd import UndoCommand
from opencomputer.agent.state import SessionDB
from opencomputer.cli_ui.slash import resolve_command
from opencomputer.cli_ui.slash_handlers import SlashContext, dispatch_slash
from plugin_sdk.core import Message
from plugin_sdk.runtime_context import RuntimeContext


def _db_with(tmp_path, sid, messages):
    """A real SessionDB seeded with ``messages`` for session ``sid``."""
    db = SessionDB(tmp_path / "sessions.db")
    db.create_session(sid)
    for m in messages:
        db.append_message(sid, m)
    return db


def _runtime(sid, db):
    return RuntimeContext(custom={"session_id": sid, "session_db": db})


# ---------- UndoCommand logic (agent SlashCommand) ----------


def test_undo_is_registered_in_builtin_commands():
    """/undo is wired into the built-in agent slash registry."""
    names = {getattr(c, "name", "") for c in get_registered_commands()}
    assert "undo" in names


@pytest.mark.asyncio
async def test_undo_removes_the_last_exchange(tmp_path):
    """The most recent user prompt + assistant reply pair is removed."""
    db = _db_with(tmp_path, "s1", [
        Message(role="user", content="first question"),
        Message(role="assistant", content="first answer"),
        Message(role="user", content="second question"),
        Message(role="assistant", content="second answer"),
    ])

    result = await UndoCommand().execute("", _runtime("s1", db))

    assert result.handled is True
    assert "removed" in result.output.lower()
    assert [m.content for m in db.get_messages("s1")] == [
        "first question",
        "first answer",
    ]


@pytest.mark.asyncio
async def test_undo_removes_tool_messages_with_the_exchange(tmp_path):
    """A tool-using turn is removed whole — no orphaned tool rows left behind."""
    db = _db_with(tmp_path, "s1", [
        Message(role="user", content="q1"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="use a tool"),
        Message(role="assistant", content="calling tool"),
        Message(role="tool", content="tool output", tool_call_id="t1"),
        Message(role="assistant", content="final answer"),
    ])

    await UndoCommand().execute("", _runtime("s1", db))

    remaining = db.get_messages("s1")
    assert [m.role for m in remaining] == ["user", "assistant"]
    assert [m.content for m in remaining] == ["q1", "a1"]


@pytest.mark.asyncio
async def test_undo_with_no_active_session_is_a_friendly_noop():
    """No session_id/session_db in runtime → friendly message, no crash."""
    result = await UndoCommand().execute("", RuntimeContext(custom={}))

    assert result.handled is True
    assert "no active session" in result.output.lower()


@pytest.mark.asyncio
async def test_undo_with_nothing_to_undo(tmp_path):
    """A session with no user message → friendly 'nothing to undo', untouched."""
    db = _db_with(tmp_path, "s1", [
        Message(role="system", content="you are helpful"),
    ])

    result = await UndoCommand().execute("", _runtime("s1", db))

    assert result.handled is True
    assert "nothing to undo" in result.output.lower()
    assert len(db.get_messages("s1")) == 1  # system message untouched


# ---------- cli_ui bridge (reachable from `oc chat`) ----------


def _slash_context(console, on_undo):
    return SlashContext(
        console=console,
        session_id="s1",
        config=MagicMock(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {"in": 0, "out": 0},
        get_session_list=list,
        on_undo=on_undo,
    )


def test_undo_is_in_cli_ui_registry():
    """`/undo` is discoverable in the oc-chat REPL slash registry."""
    cmd = resolve_command("undo")
    assert cmd is not None
    assert cmd.name == "undo"


def test_dispatch_undo_calls_on_undo_callback():
    """`/undo` in the REPL delegates to the on_undo bridge and prints its result."""
    console = Console(record=True)
    ctx = _slash_context(
        console,
        on_undo=lambda: "↩ Removed the last exchange (2 messages).",
    )

    result = dispatch_slash("/undo", ctx)

    assert result.handled is True
    assert "removed the last exchange" in console.export_text().lower()
