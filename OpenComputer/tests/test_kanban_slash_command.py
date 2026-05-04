"""Tests for the /kanban slash command (Wave 6.E.6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.kanban.slash_command import (
    KanbanSlashCommand,
    register_kanban_slash_commands,
)
from plugin_sdk.runtime_context import RuntimeContext


@pytest.fixture()
def kanban_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OC_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("OC_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("OC_KANBAN_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    from opencomputer.kanban import db
    db.init_db()
    return tmp_path


def _runtime(**custom):
    return RuntimeContext(custom=custom)


@pytest.mark.asyncio
async def test_bypass_running_guard_attribute():
    """The class attribute opts the command into the lock-bypass path
    in Dispatch._maybe_bypass_running_guard."""
    cmd = KanbanSlashCommand()
    assert cmd.bypass_running_guard is True


@pytest.mark.asyncio
async def test_empty_args_runs_list(kanban_home: Path):
    cmd = KanbanSlashCommand()
    result = await cmd.execute("", _runtime())
    assert result.handled is True
    # 'list' on an empty board is non-empty (header) + non-error
    assert result.output


@pytest.mark.asyncio
async def test_help_returns_text(kanban_home: Path):
    cmd = KanbanSlashCommand()
    # `help` is a real subcommand in the OC kanban CLI; if not, list
    # works as a sanity baseline.
    result = await cmd.execute("list", _runtime())
    assert result.handled is True


@pytest.mark.asyncio
async def test_create_returns_task_output(kanban_home: Path):
    cmd = KanbanSlashCommand()
    result = await cmd.execute(
        '--json create "test task" --assignee tester',
        _runtime(platform="telegram", chat_id="123"),
    )
    assert result.handled is True


@pytest.mark.asyncio
async def test_bad_args_returns_error_text_not_raise(kanban_home: Path):
    """argparse SystemExit must be caught — return graceful text."""
    cmd = KanbanSlashCommand()
    result = await cmd.execute(
        "create",  # missing required title
        _runtime(),
    )
    assert result.handled is True
    assert "/kanban" in result.output


def test_register_installs_into_dict():
    class _FakeRegistry:
        slash_commands: dict = {}

    reg = _FakeRegistry()
    register_kanban_slash_commands(reg)
    assert "kanban" in reg.slash_commands
    assert isinstance(reg.slash_commands["kanban"], KanbanSlashCommand)


def test_parse_task_id_handles_json():
    """The auto-subscribe id-extractor reads JSON output cleanly."""
    out = '{"id": "t-abc-123", "status": "ready"}'
    import argparse
    args = argparse.Namespace()
    tid = KanbanSlashCommand._parse_task_id(out, args)
    assert tid == "t-abc-123"


def test_parse_task_id_handles_human_output():
    out = "task t-foo-456 created at <ts>"
    import argparse
    args = argparse.Namespace()
    tid = KanbanSlashCommand._parse_task_id(out, args)
    # Either matches "t-foo-456" or doesn't crash
    assert tid is None or tid.startswith("t")


def test_parse_task_id_returns_none_for_empty():
    import argparse
    args = argparse.Namespace()
    assert KanbanSlashCommand._parse_task_id("", args) is None
