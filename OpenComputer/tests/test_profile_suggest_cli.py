"""Tests for /profile-suggest slash command."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.agent.slash_commands_impl.profile_suggest_cmd import (
    ProfileSuggestCommand,
    _resolve_available_profiles,
    _resolve_current_profile,
)
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommandResult


def _runtime_with_db(db) -> RuntimeContext:
    rt = RuntimeContext()
    rt.custom["session_db"] = db
    return rt


def test_command_name_and_description():
    cmd = ProfileSuggestCommand()
    assert cmd.name == "profile-suggest"
    assert "profile" in cmd.description.lower()


def test_execute_returns_error_when_no_db_in_runtime():
    cmd = ProfileSuggestCommand()
    rt = RuntimeContext()
    result = asyncio.run(cmd.execute("", rt))
    assert isinstance(result, SlashCommandResult)
    assert result.handled is True
    assert "no active session" in result.output.lower()


def test_execute_renders_report_when_db_present(tmp_path):
    cmd = ProfileSuggestCommand()
    db = MagicMock()
    db.list_sessions.return_value = []
    rt = _runtime_with_db(db)
    result = asyncio.run(cmd.execute("", rt))
    assert isinstance(result, SlashCommandResult)
    assert "Active profile" in result.output


def test_execute_handles_db_failure_gracefully():
    cmd = ProfileSuggestCommand()
    db = MagicMock()
    db.list_sessions.side_effect = RuntimeError("boom")
    rt = _runtime_with_db(db)
    result = asyncio.run(cmd.execute("", rt))
    assert "failed" in result.output.lower()


def test_resolve_current_profile_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    # Patch read_active_profile to return None → fallback to "default"
    with patch("opencomputer.profiles.read_active_profile", return_value=None):
        result = _resolve_current_profile()
    assert result == "default"


def test_resolve_current_profile_extracts_name_from_env(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles" / "stock"
    profiles_dir.mkdir(parents=True)
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(profiles_dir))
    result = _resolve_current_profile()
    assert result == "stock"


def test_resolve_available_profiles_always_includes_default():
    profiles = _resolve_available_profiles()
    assert "default" in profiles


def test_resolve_available_profiles_no_duplicates(monkeypatch):
    """If list_profiles returns 'default' too, it shouldn't be duplicated."""
    with patch(
        "opencomputer.profiles.list_profiles",
        return_value=["default", "stock"],
    ):
        profiles = _resolve_available_profiles()
    assert profiles.count("default") == 1
    assert "stock" in profiles


def test_command_registered_in_builtin_list():
    """The command must appear in the slash_commands.py registry."""
    from opencomputer.agent.slash_commands import _BUILTIN_COMMANDS
    cmd_names = [cls().name for cls in _BUILTIN_COMMANDS]
    assert "profile-suggest" in cmd_names
