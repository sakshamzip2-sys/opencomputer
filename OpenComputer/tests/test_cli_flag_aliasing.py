"""--yolo / --auto / --accept-edits / --plan flag aliasing + cron precedence."""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from opencomputer.cli import _derive_permission_mode, app
from plugin_sdk import PermissionMode

# Typer renders help in CI's TTY environment with ANSI color codes that
# break the dashes apart (e.g. ``-\x1b[1;36m-auto`` instead of ``--auto``)
# — locally Typer detects non-TTY and renders plain text, so the same
# substring check passes locally but fails in CI. Strip ANSI before the
# ``in`` check.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestCodeFlags:
    def test_help_lists_all_modes(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["code", "--help"])
        assert result.exit_code == 0
        out = _strip_ansi(result.stdout)
        assert "--auto" in out
        assert "--accept-edits" in out
        assert "--plan" in out
        # --yolo still listed (deprecated alias).
        assert "--yolo" in out

    def test_chat_help_lists_all_modes(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["chat", "--help"])
        assert result.exit_code == 0
        out = _strip_ansi(result.stdout)
        assert "--auto" in out
        assert "--accept-edits" in out
        assert "--yolo" in out

    def test_resume_help_lists_all_modes(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["resume", "--help"])
        assert result.exit_code == 0
        out = _strip_ansi(result.stdout)
        assert "--auto" in out
        assert "--accept-edits" in out


class TestDerivePermissionMode:
    def test_default(self) -> None:
        assert _derive_permission_mode(plan=False, auto=False, accept_edits=False) == PermissionMode.DEFAULT

    def test_plan_wins_over_auto(self) -> None:
        assert _derive_permission_mode(plan=True, auto=True, accept_edits=False) == PermissionMode.PLAN

    def test_auto_wins_over_accept_edits(self) -> None:
        assert _derive_permission_mode(plan=False, auto=True, accept_edits=True) == PermissionMode.AUTO

    def test_accept_edits(self) -> None:
        assert _derive_permission_mode(plan=False, auto=False, accept_edits=True) == PermissionMode.ACCEPT_EDITS
