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
    from unittest.mock import AsyncMock
    mock_loop = MagicMock()
    mock_loop.run_conversation = AsyncMock(
        return_value=MagicMock(final_message=MagicMock(content=final_text)),
    )
    mock_provider = MagicMock()
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
    """``--model`` reaches ``AgentLoop(config=...)``.

    Uses a real frozen ``Config`` (not a mutable mock) so the override is
    exercised through ``dataclasses.replace`` — the actual production path.
    """
    from opencomputer.agent.config import Config

    mock_loop, mock_provider = _patch_chain()
    captured: dict[str, object] = {}

    def _capture_loop(*args, **kwargs):
        captured["config"] = kwargs.get("config")
        return mock_loop

    with (
        patch("opencomputer.cli.load_config", return_value=Config()),
        patch("opencomputer.cli._check_provider_key"),
        patch("opencomputer.cli._register_builtin_tools"),
        patch("opencomputer.cli._discover_plugins"),
        patch("opencomputer.cli._apply_model_overrides"),
        patch("opencomputer.cli._discover_and_register_agents"),
        patch("opencomputer.cli._register_settings_hooks"),
        patch("opencomputer.cli._resolve_provider", return_value=mock_provider),
        patch("opencomputer.cli._configure_logging_once"),
        patch("opencomputer.agent.loop.AgentLoop", side_effect=_capture_loop),
        patch("opencomputer.tools.delegate.DelegateTool"),
    ):
        result = runner.invoke(app, ["oneshot", "x", "--model", "override-model"])
    assert result.exit_code == 0, result.output
    assert captured["config"].model.model == "override-model"  # type: ignore[union-attr]


def test_oneshot_provider_override_propagates(runner):
    """``--provider`` reaches ``AgentLoop(config=...)`` via a real ``Config``."""
    from opencomputer.agent.config import Config

    mock_loop, mock_provider = _patch_chain()
    captured: dict[str, object] = {}

    def _capture_loop(*args, **kwargs):
        captured["config"] = kwargs.get("config")
        return mock_loop

    with (
        patch("opencomputer.cli.load_config", return_value=Config()),
        patch("opencomputer.cli._check_provider_key"),
        patch("opencomputer.cli._register_builtin_tools"),
        patch("opencomputer.cli._discover_plugins"),
        patch("opencomputer.cli._apply_model_overrides"),
        patch("opencomputer.cli._discover_and_register_agents"),
        patch("opencomputer.cli._register_settings_hooks"),
        patch("opencomputer.cli._resolve_provider", return_value=mock_provider),
        patch("opencomputer.cli._configure_logging_once"),
        patch("opencomputer.agent.loop.AgentLoop", side_effect=_capture_loop),
        patch("opencomputer.tools.delegate.DelegateTool"),
    ):
        result = runner.invoke(app, ["oneshot", "x", "--provider", "openai"])
    assert result.exit_code == 0, result.output
    assert captured["config"].model.provider == "openai"  # type: ignore[union-attr]


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


def test_oneshot_model_override_real_config_does_not_crash(runner):
    """``--model`` must not crash on the real frozen ``ModelConfig``.

    Regression: ``_run_oneshot_turn`` did ``cfg.model.model = model``, but
    ``ModelConfig`` is ``@dataclass(frozen=True, slots=True)`` — so the
    assignment raised an uncaught ``FrozenInstanceError``. The earlier
    smoke tests use a ``MagicMock`` for ``cfg`` (mutable), masking the bug.
    This test drives a real ``Config`` and asserts the override reaches
    ``AgentLoop(config=...)``.
    """
    from opencomputer.agent.config import Config

    mock_loop, mock_provider = _patch_chain()
    real_cfg = Config()
    captured: dict[str, object] = {}

    def _capture_loop(*args, **kwargs):
        captured["config"] = kwargs.get("config")
        return mock_loop

    with (
        patch("opencomputer.cli.load_config", return_value=real_cfg),
        patch("opencomputer.cli._check_provider_key"),
        patch("opencomputer.cli._register_builtin_tools"),
        patch("opencomputer.cli._discover_plugins"),
        patch("opencomputer.cli._apply_model_overrides"),
        patch("opencomputer.cli._discover_and_register_agents"),
        patch("opencomputer.cli._register_settings_hooks"),
        patch("opencomputer.cli._resolve_provider", return_value=mock_provider),
        patch("opencomputer.cli._configure_logging_once"),
        patch("opencomputer.agent.loop.AgentLoop", side_effect=_capture_loop),
        patch("opencomputer.tools.delegate.DelegateTool"),
    ):
        result = runner.invoke(app, ["oneshot", "x", "--model", "override-model"])
    assert result.exit_code == 0, result.output
    assert isinstance(captured.get("config"), Config)
    assert captured["config"].model.model == "override-model"  # type: ignore[union-attr]


def test_oneshot_provider_override_real_config_does_not_crash(runner):
    """``--provider`` must not crash on the real frozen ``ModelConfig``."""
    from opencomputer.agent.config import Config

    mock_loop, mock_provider = _patch_chain()
    real_cfg = Config()
    captured: dict[str, object] = {}

    def _capture_loop(*args, **kwargs):
        captured["config"] = kwargs.get("config")
        return mock_loop

    with (
        patch("opencomputer.cli.load_config", return_value=real_cfg),
        patch("opencomputer.cli._check_provider_key"),
        patch("opencomputer.cli._register_builtin_tools"),
        patch("opencomputer.cli._discover_plugins"),
        patch("opencomputer.cli._apply_model_overrides"),
        patch("opencomputer.cli._discover_and_register_agents"),
        patch("opencomputer.cli._register_settings_hooks"),
        patch("opencomputer.cli._resolve_provider", return_value=mock_provider),
        patch("opencomputer.cli._configure_logging_once"),
        patch("opencomputer.agent.loop.AgentLoop", side_effect=_capture_loop),
        patch("opencomputer.tools.delegate.DelegateTool"),
    ):
        result = runner.invoke(app, ["oneshot", "x", "--provider", "openai"])
    assert result.exit_code == 0, result.output
    assert isinstance(captured.get("config"), Config)
    assert captured["config"].model.provider == "openai"  # type: ignore[union-attr]


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
