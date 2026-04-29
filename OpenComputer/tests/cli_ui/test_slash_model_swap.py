"""Tests for /model mid-session swap (Sub-project C of model-agnosticism)."""
from __future__ import annotations

from unittest.mock import MagicMock

from opencomputer.cli_ui.slash_handlers import SlashContext, _handle_model


def test_handle_model_no_args_shows_current():
    """`/model` (no args) prints the current model + provider."""
    ctx = MagicMock(spec=SlashContext)
    ctx.console = MagicMock()
    ctx.config = MagicMock()
    ctx.config.model.model = "claude-opus-4-7"
    ctx.config.model.provider = "anthropic"

    result = _handle_model(ctx, [])
    assert result.handled is True
    arg = ctx.console.print.call_args.args[0]
    assert "claude-opus-4-7" in arg
    assert "anthropic" in arg


def test_handle_model_with_arg_calls_on_model_swap():
    captured: dict = {}

    def _swap(m: str) -> tuple[bool, str]:
        captured["model"] = m
        return (True, m)

    ctx = MagicMock(spec=SlashContext)
    ctx.console = MagicMock()
    ctx.on_model_swap = _swap

    result = _handle_model(ctx, ["claude-haiku-4-5-20251001"])
    assert result.handled is True
    assert captured["model"] == "claude-haiku-4-5-20251001"
    arg = ctx.console.print.call_args.args[0]
    assert "model →" in arg
    assert "claude-haiku-4-5-20251001" in arg


def test_handle_model_swap_failure_echoes_reason():
    ctx = MagicMock(spec=SlashContext)
    ctx.console = MagicMock()
    ctx.on_model_swap = lambda m: (False, f"unknown model {m!r}")

    _handle_model(ctx, ["does-not-exist"])
    arg = ctx.console.print.call_args.args[0]
    assert "swap failed" in arg
    assert "does-not-exist" in arg


def test_default_on_model_swap_returns_not_wired_message():
    """When SlashContext is built without overriding on_model_swap, the
    default returns a clear 'not wired' message rather than silently
    succeeding."""
    from rich.console import Console

    ctx = SlashContext(
        console=Console(),
        session_id="t",
        config=MagicMock(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {"in": 0, "out": 0},
        get_session_list=lambda: [],
    )
    ok, msg = ctx.on_model_swap("any-model")
    assert ok is False
    assert "not wired" in msg
