"""Tests for ``oc oneshot <prompt>`` non-interactive mode (Wave 6.A).

Hermes-port (7c8c031f6). Smoke tests at the Typer level — verifies the
command exists, accepts a prompt, accepts model/provider/plan overrides,
and properly forwards to ``AgentLoop.run_conversation``.

The full agent-loop path is covered by the existing AgentLoop tests; here
we just confirm CLI wiring + override propagation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from opencomputer.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _patch_chain(*, final_text: str = "ok"):
    """Patch the heavyweight init chain so the test runs in milliseconds.

    Returns a (mock_loop, mock_provider) pair so tests can inspect what
    run_conversation was called with.
    """
    from unittest.mock import AsyncMock, MagicMock as _MM
    mock_loop = _MM()
    mock_loop.run_conversation = AsyncMock(
        return_value=_MM(final_message=_MM(content=final_text)),
    )
    mock_provider = _MM()
    return mock_loop, mock_provider


def test_oneshot_command_exists(runner):
    """--help should list ``oneshot``."""
    result = runner.invoke(app, ["oneshot", "--help"])
    assert result.exit_code == 0
    assert "oneshot" in result.stdout.lower() or "single" in result.stdout.lower()


def test_oneshot_prints_final_text(runner):
    mock_loop, mock_provider = _patch_chain(final_text="hello world")
    with (
        patch("opencomputer.cli.load_config") as ld,
        patch("opencomputer.cli._check_provider_key"),
        patch("opencomputer.cli._register_builtin_tools"),
        patch("opencomputer.cli._discover_plugins"),
        patch("opencomputer.cli._apply_model_overrides"),
        patch("opencomputer.cli._discover_and_register_agents"),
        patch("opencomputer.cli._register_settings_hooks"),
        patch("opencomputer.cli._resolve_provider", return_value=mock_provider),
        patch("opencomputer.cli._configure_logging_once"),
        patch("opencomputer.agent.loop.AgentLoop", return_value=mock_loop),
        patch("opencomputer.tools.delegate.DelegateTool"),
    ):
        cfg = MagicMock()
        cfg.model.provider = "anthropic"
        cfg.model.model = "claude-opus-4-7"
        ld.return_value = cfg
        result = runner.invoke(app, ["oneshot", "hi"])
    assert result.exit_code == 0
    assert "hello world" in result.stdout
    # run_conversation was called with the prompt
    assert mock_loop.run_conversation.await_args.args[0] == "hi"


def test_oneshot_model_override_propagates(runner):
    mock_loop, mock_provider = _patch_chain()
    cfg = MagicMock()
    cfg.model.provider = "anthropic"
    cfg.model.model = "default-model"
    with (
        patch("opencomputer.cli.load_config", return_value=cfg),
        patch("opencomputer.cli._check_provider_key"),
        patch("opencomputer.cli._register_builtin_tools"),
        patch("opencomputer.cli._discover_plugins"),
        patch("opencomputer.cli._apply_model_overrides"),
        patch("opencomputer.cli._discover_and_register_agents"),
        patch("opencomputer.cli._register_settings_hooks"),
        patch("opencomputer.cli._resolve_provider", return_value=mock_provider),
        patch("opencomputer.cli._configure_logging_once"),
        patch("opencomputer.agent.loop.AgentLoop", return_value=mock_loop),
        patch("opencomputer.tools.delegate.DelegateTool"),
    ):
        result = runner.invoke(app, ["oneshot", "x", "--model", "override-model"])
    assert result.exit_code == 0
    assert cfg.model.model == "override-model"


def test_oneshot_provider_override_propagates(runner):
    mock_loop, mock_provider = _patch_chain()
    cfg = MagicMock()
    cfg.model.provider = "default-provider"
    cfg.model.model = "x"
    with (
        patch("opencomputer.cli.load_config", return_value=cfg),
        patch("opencomputer.cli._check_provider_key"),
        patch("opencomputer.cli._register_builtin_tools"),
        patch("opencomputer.cli._discover_plugins"),
        patch("opencomputer.cli._apply_model_overrides"),
        patch("opencomputer.cli._discover_and_register_agents"),
        patch("opencomputer.cli._register_settings_hooks"),
        patch("opencomputer.cli._resolve_provider", return_value=mock_provider),
        patch("opencomputer.cli._configure_logging_once"),
        patch("opencomputer.agent.loop.AgentLoop", return_value=mock_loop),
        patch("opencomputer.tools.delegate.DelegateTool"),
    ):
        result = runner.invoke(app, ["oneshot", "x", "--provider", "openai"])
    assert result.exit_code == 0
    assert cfg.model.provider == "openai"


def test_oneshot_plan_mode_flag(runner):
    """``--plan`` should set plan_mode=True on the runtime context."""
    mock_loop, mock_provider = _patch_chain()
    cfg = MagicMock()
    cfg.model.provider = "anthropic"
    cfg.model.model = "x"
    with (
        patch("opencomputer.cli.load_config", return_value=cfg),
        patch("opencomputer.cli._check_provider_key"),
        patch("opencomputer.cli._register_builtin_tools"),
        patch("opencomputer.cli._discover_plugins"),
        patch("opencomputer.cli._apply_model_overrides"),
        patch("opencomputer.cli._discover_and_register_agents"),
        patch("opencomputer.cli._register_settings_hooks"),
        patch("opencomputer.cli._resolve_provider", return_value=mock_provider),
        patch("opencomputer.cli._configure_logging_once"),
        patch("opencomputer.agent.loop.AgentLoop", return_value=mock_loop),
        patch("opencomputer.tools.delegate.DelegateTool"),
    ):
        result = runner.invoke(app, ["oneshot", "x", "--plan"])
    assert result.exit_code == 0
    runtime = mock_loop.run_conversation.await_args.kwargs["runtime"]
    assert runtime.plan_mode is True


def test_oneshot_empty_response_silent(runner):
    """Empty final_message → no output, exit 0."""
    mock_loop, mock_provider = _patch_chain(final_text="")
    cfg = MagicMock()
    cfg.model.provider = "anthropic"
    cfg.model.model = "x"
    with (
        patch("opencomputer.cli.load_config", return_value=cfg),
        patch("opencomputer.cli._check_provider_key"),
        patch("opencomputer.cli._register_builtin_tools"),
        patch("opencomputer.cli._discover_plugins"),
        patch("opencomputer.cli._apply_model_overrides"),
        patch("opencomputer.cli._discover_and_register_agents"),
        patch("opencomputer.cli._register_settings_hooks"),
        patch("opencomputer.cli._resolve_provider", return_value=mock_provider),
        patch("opencomputer.cli._configure_logging_once"),
        patch("opencomputer.agent.loop.AgentLoop", return_value=mock_loop),
        patch("opencomputer.tools.delegate.DelegateTool"),
    ):
        result = runner.invoke(app, ["oneshot", "x"])
    assert result.exit_code == 0
    assert result.stdout.strip() == ""
