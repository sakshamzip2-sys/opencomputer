"""Tests for ``oc chat`` Hermes-parity aliases — ``-c`` (continue) and ``-q`` (query).

Mirrors hermes-agent's ``hermes chat -c`` (resume latest) and ``hermes chat -q "..."``
(non-interactive single turn) ergonomic aliases. Both delegate to existing OC paths:
``-c`` → ``--resume last``, ``-q`` → ``oc oneshot``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from opencomputer.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_chat_q_runs_oneshot_path(runner):
    """``oc chat -q "hello"`` should invoke the shared oneshot helper, not enter the REPL."""
    with patch("opencomputer.cli._run_oneshot_turn") as helper, patch(
        "opencomputer.cli._run_chat_session"
    ) as repl:
        result = runner.invoke(app, ["chat", "-q", "hello world"])
    assert result.exit_code == 0
    helper.assert_called_once()
    # First positional arg is the prompt; plan kwarg defaults False.
    args, kwargs = helper.call_args
    assert args[0] == "hello world"
    assert kwargs.get("plan", False) is False
    # REPL path must not be taken when -q is used.
    repl.assert_not_called()


def test_chat_q_with_plan_propagates(runner):
    """``oc chat -q "..." --plan`` should pass plan=True to the oneshot helper."""
    with patch("opencomputer.cli._run_oneshot_turn") as helper, patch(
        "opencomputer.cli._run_chat_session"
    ):
        result = runner.invoke(app, ["chat", "--plan", "-q", "scout"])
    assert result.exit_code == 0
    helper.assert_called_once()
    _args, kwargs = helper.call_args
    assert kwargs.get("plan") is True


def test_chat_c_short_flag_resumes_last(runner):
    """``oc chat -c`` should set resume='last' when no explicit --resume given."""
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)

    with patch("opencomputer.cli._run_chat_session", side_effect=_capture):
        result = runner.invoke(app, ["chat", "-c"])
    assert result.exit_code == 0
    assert captured.get("resume") == "last"


def test_chat_continue_long_flag_resumes_last(runner):
    """``oc chat --continue`` is the long form of -c."""
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)

    with patch("opencomputer.cli._run_chat_session", side_effect=_capture):
        result = runner.invoke(app, ["chat", "--continue"])
    assert result.exit_code == 0
    assert captured.get("resume") == "last"


def test_chat_explicit_resume_beats_continue(runner):
    """If both ``-c`` and ``--resume <id>`` are given, the explicit id wins."""
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)

    with patch("opencomputer.cli._run_chat_session", side_effect=_capture):
        result = runner.invoke(app, ["chat", "-c", "--resume", "abc12345"])
    assert result.exit_code == 0
    assert captured.get("resume") == "abc12345"


def test_run_oneshot_turn_helper_uses_real_loop(runner):
    """The shared helper drives AgentLoop.run_conversation with the prompt.

    This mirrors the original test_cli_oneshot.py shape but exercises the
    extracted helper directly so future ``-q`` callers stay covered.
    """
    mock_loop = MagicMock()
    mock_loop.run_conversation = AsyncMock(
        return_value=MagicMock(final_message=MagicMock(content="result"))
    )
    cfg = MagicMock()
    cfg.model.provider = "anthropic"
    cfg.model.model = "claude-x"
    with (
        patch("opencomputer.cli.load_config", return_value=cfg),
        patch("opencomputer.cli._check_provider_key"),
        patch("opencomputer.cli._register_builtin_tools"),
        patch("opencomputer.cli._discover_plugins"),
        patch("opencomputer.cli._apply_model_overrides"),
        patch("opencomputer.cli._discover_and_register_agents"),
        patch("opencomputer.cli._register_settings_hooks"),
        patch("opencomputer.cli._resolve_provider", return_value=MagicMock()),
        patch("opencomputer.cli._configure_logging_once"),
        patch("opencomputer.agent.loop.AgentLoop", return_value=mock_loop),
        patch("opencomputer.tools.delegate.DelegateTool"),
    ):
        result = runner.invoke(app, ["chat", "-q", "smoke"])
    assert result.exit_code == 0
    assert "result" in result.stdout
    assert mock_loop.run_conversation.await_args.args[0] == "smoke"
