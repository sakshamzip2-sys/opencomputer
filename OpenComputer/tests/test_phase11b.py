"""Phase 11b: Claude Code tool parity (core slice).

Tests for the four new core tools (NotebookEdit, AskUserQuestion,
PushNotification, Skill) plus the SDK surface additions (interaction types,
3 new HookEvent values, BaseChannelAdapter.send_notification default).

Coding-harness rows (ExitPlanMode, Monitor, BashOutput/KillShell) ship in a
follow-up PR after the parallel coding-harness session signals done.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import Platform, SendResult, ToolCall

# ─── plugin_sdk surface additions ──────────────────────────────────────


def test_interaction_types_exported_from_plugin_sdk_root() -> None:
    from plugin_sdk import InteractionRequest, InteractionResponse

    req = InteractionRequest(question="hi?", options=("a", "b"))
    resp = InteractionResponse(text="a", option_index=0)
    assert req.question == "hi?"
    assert req.options == ("a", "b")
    assert req.presentation == "text"  # default
    assert resp.text == "a"
    assert resp.option_index == 0


def test_three_new_hook_events_exist_and_are_in_all_hook_events() -> None:
    from plugin_sdk import ALL_HOOK_EVENTS, HookEvent

    assert HookEvent.PRE_COMPACT.value == "PreCompact"
    assert HookEvent.SUBAGENT_STOP.value == "SubagentStop"
    assert HookEvent.NOTIFICATION.value == "Notification"
    assert HookEvent.PRE_COMPACT in ALL_HOOK_EVENTS
    assert HookEvent.SUBAGENT_STOP in ALL_HOOK_EVENTS
    assert HookEvent.NOTIFICATION in ALL_HOOK_EVENTS
    # Phase 11b shipped 9 events; Round 2A P-1 adds 8 more for 17;
    # Wave 5 T13/T14 adds 3 (PRE_GATEWAY_DISPATCH, PRE/POST_APPROVAL_*) for 20.
    assert len(ALL_HOOK_EVENTS) == 20


async def test_base_channel_adapter_send_notification_default_routes_to_send() -> None:
    """A channel adapter that doesn't override send_notification falls back to send()."""
    from plugin_sdk.channel_contract import BaseChannelAdapter

    class _Adapter(BaseChannelAdapter):
        platform = Platform.CLI

        def __init__(self) -> None:
            super().__init__(config={})
            self.sends: list[tuple[str, str]] = []

        async def connect(self) -> bool:
            return True

        async def disconnect(self) -> None:
            return None

        async def send(self, chat_id: str, text: str, **kwargs):
            self.sends.append((chat_id, text))
            return SendResult(success=True, message_id="m-1")

    adapter = _Adapter()
    result = await adapter.send_notification("c1", "ping", urgent=True)
    assert result.success
    # Default routes through send — same payload, no notification-specific path
    assert adapter.sends == [("c1", "ping")]


# ─── NotebookEdit ──────────────────────────────────────────────────────


def _empty_notebook(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "cells": [],
                "metadata": {"kernelspec": {"name": "python3"}},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(id="tc-1", name=name, arguments=args)


async def test_notebook_edit_insert_appends_cell(tmp_path: Path) -> None:
    from opencomputer.tools.notebook_edit import NotebookEditTool

    nb_path = tmp_path / "test.ipynb"
    _empty_notebook(nb_path)
    tool = NotebookEditTool()

    res = await tool.execute(
        _call(
            "NotebookEdit",
            {
                "path": str(nb_path),
                "mode": "insert",
                "cell_index": 0,
                "cell_type": "code",
                "source": "print('hello')",
            },
        )
    )
    assert not res.is_error
    nb = json.loads(nb_path.read_text())
    assert len(nb["cells"]) == 1
    assert nb["cells"][0]["cell_type"] == "code"
    assert nb["cells"][0]["source"] == "print('hello')"
    assert "id" in nb["cells"][0]


async def test_notebook_edit_replace_changes_cell_in_place(tmp_path: Path) -> None:
    from opencomputer.tools.notebook_edit import NotebookEditTool

    nb_path = tmp_path / "test.ipynb"
    _empty_notebook(nb_path)
    tool = NotebookEditTool()

    # Insert one, then replace it
    await tool.execute(
        _call(
            "NotebookEdit",
            {
                "path": str(nb_path),
                "mode": "insert",
                "cell_index": 0,
                "cell_type": "markdown",
                "source": "# old",
            },
        )
    )
    res = await tool.execute(
        _call(
            "NotebookEdit",
            {
                "path": str(nb_path),
                "mode": "replace",
                "cell_index": 0,
                "cell_type": "markdown",
                "source": "# new",
            },
        )
    )
    assert not res.is_error
    nb = json.loads(nb_path.read_text())
    assert len(nb["cells"]) == 1
    assert nb["cells"][0]["source"] == "# new"


async def test_notebook_edit_delete_removes_cell(tmp_path: Path) -> None:
    from opencomputer.tools.notebook_edit import NotebookEditTool

    nb_path = tmp_path / "test.ipynb"
    _empty_notebook(nb_path)
    tool = NotebookEditTool()

    for src in ("a", "b", "c"):
        await tool.execute(
            _call(
                "NotebookEdit",
                {
                    "path": str(nb_path),
                    "mode": "insert",
                    "cell_index": len(json.loads(nb_path.read_text())["cells"]),
                    "cell_type": "code",
                    "source": src,
                },
            )
        )
    res = await tool.execute(
        _call(
            "NotebookEdit",
            {"path": str(nb_path), "mode": "delete", "cell_index": 1},
        )
    )
    assert not res.is_error
    nb = json.loads(nb_path.read_text())
    assert [c["source"] for c in nb["cells"]] == ["a", "c"]


async def test_notebook_edit_rejects_non_ipynb(tmp_path: Path) -> None:
    from opencomputer.tools.notebook_edit import NotebookEditTool

    bad = tmp_path / "test.txt"
    bad.write_text("not a notebook")
    tool = NotebookEditTool()
    res = await tool.execute(
        _call(
            "NotebookEdit",
            {
                "path": str(bad),
                "mode": "insert",
                "cell_index": 0,
                "cell_type": "code",
                "source": "x",
            },
        )
    )
    assert res.is_error
    assert ".ipynb" in res.content


async def test_notebook_edit_rejects_out_of_bounds_index(tmp_path: Path) -> None:
    from opencomputer.tools.notebook_edit import NotebookEditTool

    nb_path = tmp_path / "test.ipynb"
    _empty_notebook(nb_path)
    tool = NotebookEditTool()
    res = await tool.execute(
        _call(
            "NotebookEdit",
            {"path": str(nb_path), "mode": "delete", "cell_index": 5},
        )
    )
    assert res.is_error
    assert "out of bounds" in res.content


# ─── Skill (invocable) ─────────────────────────────────────────────────


async def test_skill_tool_returns_body_for_known_skill(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.tools.skill import SkillTool

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "do-x").mkdir()
    (skills_dir / "do-x" / "SKILL.md").write_text(
        "---\nname: do-x\ndescription: How to X\n---\n\n## Steps\n1. Do it.\n"
    )

    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=skills_dir)
    tool = SkillTool(memory_manager=mem)
    res = await tool.execute(_call("Skill", {"name": "do-x"}))
    assert not res.is_error
    assert "How to X" not in res.content  # frontmatter description not in body
    assert "Do it." in res.content
    assert "do-x" in res.content


async def test_skill_tool_unknown_name_lists_available(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.tools.skill import SkillTool

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "alpha").mkdir()
    (skills_dir / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: a\n---\nbody\n")
    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=skills_dir)
    tool = SkillTool(memory_manager=mem)
    res = await tool.execute(_call("Skill", {"name": "missing"}))
    assert res.is_error
    assert "alpha" in res.content


# ─── PushNotification ──────────────────────────────────────────────────


async def test_push_notification_cli_mode_prints_to_stdout(capsys) -> None:
    from opencomputer.tools.push_notification import PushNotificationTool

    tool = PushNotificationTool(dispatch=None)
    res = await tool.execute(_call("PushNotification", {"text": "build done"}))
    assert not res.is_error
    captured = capsys.readouterr()
    assert "[NOTIFICATION] build done" in captured.out


async def test_push_notification_gateway_mode_routes_via_adapter() -> None:
    from opencomputer.tools.push_notification import PushNotificationTool

    fake_adapter = MagicMock()
    fake_adapter.send_notification = AsyncMock(
        return_value=SendResult(success=True, message_id="m-99")
    )
    fake_dispatch = MagicMock()
    fake_dispatch._adapters_by_platform = {"telegram": fake_adapter}

    tool = PushNotificationTool(dispatch=fake_dispatch)
    res = await tool.execute(
        _call("PushNotification", {"text": "ping", "chat_id": "c-7", "urgent": True})
    )
    assert not res.is_error
    fake_adapter.send_notification.assert_awaited_once_with("c-7", "ping", urgent=True)
    assert "m-99" in res.content


async def test_push_notification_gateway_mode_requires_chat_id() -> None:
    from opencomputer.tools.push_notification import PushNotificationTool

    fake_adapter = MagicMock()
    fake_dispatch = MagicMock()
    fake_dispatch._adapters_by_platform = {"telegram": fake_adapter}

    tool = PushNotificationTool(dispatch=fake_dispatch)
    res = await tool.execute(_call("PushNotification", {"text": "ping"}))
    assert res.is_error
    assert "chat_id" in res.content


# ─── AskUserQuestion ───────────────────────────────────────────────────


async def test_ask_user_question_cli_reads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.tools.ask_user_question import AskUserQuestionTool

    monkeypatch.setattr("sys.stdin", io.StringIO("Saksham\n"))
    monkeypatch.setattr("sys.stderr", io.StringIO())  # silence prompt
    tool = AskUserQuestionTool(cli_mode=True)
    res = await tool.execute(_call("AskUserQuestion", {"question": "What is your name?"}))
    assert not res.is_error
    assert "Saksham" in res.content


async def test_ask_user_question_cli_numeric_choice_expands_to_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.tools.ask_user_question import AskUserQuestionTool

    monkeypatch.setattr("sys.stdin", io.StringIO("2\n"))
    monkeypatch.setattr("sys.stderr", io.StringIO())
    tool = AskUserQuestionTool(cli_mode=True)
    res = await tool.execute(
        _call(
            "AskUserQuestion",
            {"question": "Choose colour", "options": ["red", "green", "blue"]},
        )
    )
    assert not res.is_error
    # Picked option 2 = green
    assert "green" in res.content
    assert "option 2" in res.content


async def test_ask_user_question_async_channel_returns_helpful_error() -> None:
    from opencomputer.tools.ask_user_question import AskUserQuestionTool

    tool = AskUserQuestionTool(cli_mode=False)
    res = await tool.execute(_call("AskUserQuestion", {"question": "X?"}))
    assert res.is_error
    assert "Phase 11e" in res.content
    assert "PushNotification" in res.content


async def test_ask_user_question_rejects_empty_question() -> None:
    from opencomputer.tools.ask_user_question import AskUserQuestionTool

    tool = AskUserQuestionTool(cli_mode=True)
    res = await tool.execute(_call("AskUserQuestion", {"question": ""}))
    assert res.is_error
    assert "required" in res.content


# ─── Registry registration ─────────────────────────────────────────────


def test_register_builtin_tools_includes_phase11b_tools() -> None:
    """All four core 11b tools must be registered alongside existing builtins."""
    from opencomputer.cli import _register_builtin_tools
    from opencomputer.tools.registry import registry

    _register_builtin_tools()
    names = set(registry.names())
    assert "NotebookEdit" in names
    assert "Skill" in names
    assert "PushNotification" in names
    assert "AskUserQuestion" in names
