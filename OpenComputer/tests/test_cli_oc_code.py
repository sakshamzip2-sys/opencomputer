"""V3.A-T7 — ``oc code`` command tests.

Verifies the snappy ``oc code [path]`` entry-point matching ``claude``
ergonomics. The shorthand ``oc`` shell alias is registered via
``[project.scripts]`` in ``pyproject.toml`` and requires reinstall to take
effect — these tests only validate that the command is exposed on the Typer
app (which both ``opencomputer`` and ``oc`` console-scripts dispatch to).
"""
from __future__ import annotations

from typer.testing import CliRunner

from opencomputer.cli import app

runner = CliRunner()


def test_code_command_exists() -> None:
    """``oc code --help`` returns a 0 exit code and mentions coding."""
    result = runner.invoke(app, ["code", "--help"])
    assert result.exit_code == 0, result.stdout
    output = result.stdout.lower()
    assert "code" in output or "coding" in output


def test_code_accepts_path_argument(tmp_path) -> None:
    """``oc code <path> --help`` should accept a positional path."""
    result = runner.invoke(app, ["code", str(tmp_path), "--help"])
    assert result.exit_code == 0, result.stdout


def test_code_supports_plan_flag() -> None:
    """``oc code`` exposes ``--plan`` for read-only discovery mode."""
    result = runner.invoke(app, ["code", "--help"])
    assert "--plan" in result.stdout


def test_code_supports_yolo_flag() -> None:
    """``oc code`` exposes ``--yolo`` to skip per-action confirmation prompts."""
    result = runner.invoke(app, ["code", "--help"])
    assert "--yolo" in result.stdout
