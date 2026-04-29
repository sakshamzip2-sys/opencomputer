"""--yolo / --auto / --accept-edits / --plan flag aliasing + cron precedence."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from opencomputer.cli import _derive_permission_mode, app
from plugin_sdk import PermissionMode


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestCodeFlags:
    def test_help_lists_all_modes(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["code", "--help"])
        assert result.exit_code == 0
        assert "--auto" in result.stdout
        assert "--accept-edits" in result.stdout
        assert "--plan" in result.stdout
        # --yolo still listed (deprecated alias).
        assert "--yolo" in result.stdout

    def test_chat_help_lists_all_modes(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["chat", "--help"])
        assert result.exit_code == 0
        assert "--auto" in result.stdout
        assert "--accept-edits" in result.stdout
        assert "--yolo" in result.stdout

    def test_resume_help_lists_all_modes(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["resume", "--help"])
        assert result.exit_code == 0
        assert "--auto" in result.stdout
        assert "--accept-edits" in result.stdout


class TestDerivePermissionMode:
    def test_default(self) -> None:
        assert _derive_permission_mode(plan=False, auto=False, accept_edits=False) == PermissionMode.DEFAULT

    def test_plan_wins_over_auto(self) -> None:
        assert _derive_permission_mode(plan=True, auto=True, accept_edits=False) == PermissionMode.PLAN

    def test_auto_wins_over_accept_edits(self) -> None:
        assert _derive_permission_mode(plan=False, auto=True, accept_edits=True) == PermissionMode.AUTO

    def test_accept_edits(self) -> None:
        assert _derive_permission_mode(plan=False, auto=False, accept_edits=True) == PermissionMode.ACCEPT_EDITS
