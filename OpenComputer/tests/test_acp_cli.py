"""PR-D: verify `opencomputer acp` subcommand registers."""
from typer.testing import CliRunner

from opencomputer.cli import app


def test_acp_subcommand_listed():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "acp" in result.output


def test_acp_help_works():
    runner = CliRunner()
    result = runner.invoke(app, ["acp", "--help"])
    assert result.exit_code == 0
    assert "Agent Client Protocol" in result.output
