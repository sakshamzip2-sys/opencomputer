"""Smoke tests for the ``oc resume`` Typer subcommand.

Full picker UI is interactive and untestable in CI; these tests verify
the command is registered and ``--help`` parses.
"""
from __future__ import annotations

from typer.testing import CliRunner

from opencomputer.cli import app

runner = CliRunner()


def test_resume_command_registered():
    result = runner.invoke(app, ["resume", "--help"])
    assert result.exit_code == 0
    assert "resume" in result.stdout.lower()


def test_resume_listed_in_top_level_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "resume" in result.stdout.lower()
