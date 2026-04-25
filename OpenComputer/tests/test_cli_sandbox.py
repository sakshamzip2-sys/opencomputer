"""CLI tests for ``opencomputer sandbox`` subapp (Phase 3.E)."""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from opencomputer.cli_sandbox import sandbox_app
from opencomputer.sandbox import (
    DockerStrategy,
    LinuxBwrapStrategy,
    MacOSSandboxExecStrategy,
)
from plugin_sdk.sandbox import SandboxResult, SandboxUnavailable

runner = CliRunner()


def test_status_prints_each_strategy() -> None:
    """``sandbox status`` lists all four strategies."""
    result = runner.invoke(sandbox_app, ["status"])
    assert result.exit_code == 0, result.output
    for name in ("macos_sandbox_exec", "linux_bwrap", "docker", "none"):
        assert name in result.output


def test_status_handles_no_available_strategy() -> None:
    """When everything is unavailable, the auto-pick line surfaces the SandboxUnavailable."""
    with (
        patch.object(MacOSSandboxExecStrategy, "is_available", return_value=False),
        patch.object(LinuxBwrapStrategy, "is_available", return_value=False),
        patch.object(DockerStrategy, "is_available", return_value=False),
    ):
        result = runner.invoke(sandbox_app, ["status"])
    assert result.exit_code == 0  # status itself never fails
    assert "auto unavailable" in result.output


def test_explain_prints_wrapped_argv() -> None:
    """``sandbox explain`` prints each token on its own line."""
    # Force docker as the auto pick so output is host-independent.
    with (
        patch.object(MacOSSandboxExecStrategy, "is_available", return_value=False),
        patch.object(LinuxBwrapStrategy, "is_available", return_value=False),
        patch.object(DockerStrategy, "is_available", return_value=True),
    ):
        result = runner.invoke(sandbox_app, ["explain", "echo", "hi"])
    assert result.exit_code == 0, result.output
    assert "docker" in result.output
    assert "echo" in result.output
    assert "hi" in result.output


def test_explain_errors_when_no_strategy_available() -> None:
    with (
        patch.object(MacOSSandboxExecStrategy, "is_available", return_value=False),
        patch.object(LinuxBwrapStrategy, "is_available", return_value=False),
        patch.object(DockerStrategy, "is_available", return_value=False),
    ):
        result = runner.invoke(sandbox_app, ["explain", "echo", "hi"])
    assert result.exit_code != 0


def test_run_command_returns_stdout() -> None:
    """``sandbox run -- echo hi`` prints ``hi`` and exits 0."""
    fake = SandboxResult(
        exit_code=0,
        stdout="hi\n",
        stderr="",
        duration_seconds=0.01,
        wrapped_command=["docker", "run", "--rm", "echo", "hi"],
        strategy_name="docker",
    )

    async def _fake_run_sandboxed(argv, *, config=None, stdin=None, cwd=None):  # noqa: ANN001
        return fake

    with patch("opencomputer.cli_sandbox.run_sandboxed", _fake_run_sandboxed):
        result = runner.invoke(sandbox_app, ["run", "echo", "hi"])
    assert result.exit_code == 0, result.output
    assert "hi" in result.output
    assert "strategy=docker" in result.output


def test_run_propagates_nonzero_exit() -> None:
    fake = SandboxResult(
        exit_code=2,
        stdout="",
        stderr="bad",
        duration_seconds=0.01,
        wrapped_command=["docker", "run", "--rm", "false"],
        strategy_name="docker",
    )

    async def _fake_run_sandboxed(argv, *, config=None, stdin=None, cwd=None):  # noqa: ANN001
        return fake

    with patch("opencomputer.cli_sandbox.run_sandboxed", _fake_run_sandboxed):
        result = runner.invoke(sandbox_app, ["run", "false"])
    assert result.exit_code == 2


def test_run_handles_sandbox_unavailable() -> None:
    async def _raise(*a, **kw):  # noqa: ANN001
        raise SandboxUnavailable("no sandbox available")

    with patch("opencomputer.cli_sandbox.run_sandboxed", _raise):
        result = runner.invoke(sandbox_app, ["run", "echo", "hi"])
    assert result.exit_code != 0
    assert "no sandbox available" in result.output
