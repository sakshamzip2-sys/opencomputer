"""Tests for the /update slash command."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from opencomputer.agent.slash_commands_impl.update_cmd import UpdateCommand
from plugin_sdk.runtime_context import RuntimeContext


@pytest.mark.asyncio
async def test_returns_upgrade_hint_when_newer_available():
    cmd = UpdateCommand()
    runtime = RuntimeContext()
    with patch(
        "opencomputer.cli_update_check.get_update_hint",
        return_value="A newer opencomputer (2030.1.1) is available — upgrade with: pip install -U opencomputer",
    ), patch("opencomputer.cli_update_check.prefetch_update_check"):
        result = await cmd.execute("", runtime)
    assert result.handled is True
    assert "newer opencomputer" in result.output
    assert "pip install -U" in result.output
    assert "Current:" in result.output


@pytest.mark.asyncio
async def test_returns_up_to_date_when_no_hint():
    cmd = UpdateCommand()
    runtime = RuntimeContext()
    with patch(
        "opencomputer.cli_update_check.get_update_hint",
        return_value=None,
    ), patch("opencomputer.cli_update_check.prefetch_update_check"):
        result = await cmd.execute("", runtime)
    assert result.handled is True
    assert "up to date" in result.output


@pytest.mark.asyncio
async def test_honors_opt_out_env_var(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_NO_UPDATE_CHECK", "1")
    cmd = UpdateCommand()
    runtime = RuntimeContext()
    result = await cmd.execute("", runtime)
    assert result.handled is True
    assert "disabled" in result.output


def test_command_metadata():
    assert UpdateCommand.name == "update"
    assert "newer" in UpdateCommand.description.lower() or "release" in UpdateCommand.description.lower()


def test_registered_in_builtin_list():
    from opencomputer.agent.slash_commands import _BUILTIN_COMMANDS
    assert UpdateCommand in _BUILTIN_COMMANDS
