"""B4: CLI aliases for natural-name commands users hit first."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from opencomputer.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_oc_webhooks_plural_alias_exists(runner: CliRunner) -> None:
    """`oc webhooks --help` must work the same as `oc webhook --help`."""
    plural = runner.invoke(app, ["webhooks", "--help"])
    singular = runner.invoke(app, ["webhook", "--help"])
    assert plural.exit_code == 0, plural.stdout
    assert singular.exit_code == 0, singular.stdout
    # Both list the same subcommands
    for sub in ("list", "create", "revoke"):
        assert sub in plural.stdout


def test_oc_routing_alias_of_bindings(runner: CliRunner) -> None:
    routing = runner.invoke(app, ["routing", "--help"])
    bindings = runner.invoke(app, ["bindings", "--help"])
    assert routing.exit_code == 0, routing.stdout
    assert bindings.exit_code == 0, bindings.stdout


def test_oc_eval_list_alias_for_history(runner: CliRunner) -> None:
    """`oc eval list` must not error with 'No such command'."""
    result = runner.invoke(app, ["eval", "list", "--help"])
    assert result.exit_code == 0, result.stdout
    assert "No such command" not in result.stdout


def test_oc_checkpoints_list_alias_for_status(runner: CliRunner) -> None:
    result = runner.invoke(app, ["checkpoints", "list", "--help"])
    assert result.exit_code == 0, result.stdout
    assert "No such command" not in result.stdout


def test_oc_adapter_list_subcommand_exists(runner: CliRunner) -> None:
    result = runner.invoke(app, ["adapter", "list", "--help"])
    assert result.exit_code == 0, result.stdout
    assert "No such command" not in result.stdout
