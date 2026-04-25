"""PR-1: verify `opencomputer evolution` subapp is wired into the main CLI."""
from typer.testing import CliRunner

from opencomputer.cli import app

runner = CliRunner()

def test_evolution_subcommand_listed():
    """`opencomputer --help` lists the evolution subcommand."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "evolution" in result.output

def test_evolution_help_works():
    """`opencomputer evolution --help` exits 0."""
    result = runner.invoke(app, ["evolution", "--help"])
    assert result.exit_code == 0
    assert "Self-improvement" in result.output  # from evolution_app help text
