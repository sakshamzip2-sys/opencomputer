"""Tests for /debug slash — sanitized diagnostic dump."""
from __future__ import annotations

import logging
from io import StringIO
from unittest.mock import MagicMock

from opencomputer.cli_ui.debug_dump import _TRACKED_ENV_VARS, build_debug_dump


def test_dump_starts_and_ends_with_fence():
    out = build_debug_dump()
    assert out.startswith("```")
    assert out.endswith("```")


def test_dump_includes_versions():
    out = build_debug_dump()
    assert "python:" in out
    assert "opencomputer:" in out
    assert "platform:" in out


def test_dump_includes_env_section():
    out = build_debug_dump()
    assert "=== Env vars ===" in out


def test_dump_redacts_anthropic_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-123456789-DO-NOT-LEAK")
    out = build_debug_dump()
    assert "sk-ant-secret-123456789-DO-NOT-LEAK" not in out
    assert "ANTHROPIC_API_KEY: set" in out


def test_dump_redacts_openrouter_api_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-987654321-DO-NOT-LEAK")
    out = build_debug_dump()
    assert "sk-or-secret-987654321-DO-NOT-LEAK" not in out
    assert "OPENROUTER_API_KEY: set" in out


def test_dump_marks_unset_keys_as_unset(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    out = build_debug_dump()
    assert "TELEGRAM_BOT_TOKEN: unset" in out


def test_tracked_env_vars_includes_all_provider_keys():
    """Regression: don't accidentally drop a provider key from tracking."""
    required = (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "DISCORD_BOT_TOKEN",
        "SLACK_BOT_TOKEN",
    )
    for key in required:
        assert key in _TRACKED_ENV_VARS, f"{key} missing from _TRACKED_ENV_VARS"


def test_tracked_env_vars_redacts_value_for_every_set_key(monkeypatch):
    """Defensive: every var in _TRACKED_ENV_VARS, when set, must NOT appear by value."""
    secrets = {var: f"REDACTED-PROBE-{i}" for i, var in enumerate(_TRACKED_ENV_VARS)}
    for var, val in secrets.items():
        monkeypatch.setenv(var, val)
    out = build_debug_dump()
    for var, val in secrets.items():
        assert val not in out, f"value for {var} leaked into dump"


def test_handle_debug_prints_to_console():
    """Smoke: _handle_debug calls ctx.console.print and returns handled=True."""
    from opencomputer.cli_ui.slash_handlers import _handle_debug

    ctx = MagicMock()
    ctx.console = MagicMock()
    result = _handle_debug(ctx, [])
    assert result.handled is True
    assert ctx.console.print.called
    # Captured arg should be the markdown dump
    arg = ctx.console.print.call_args.args[0]
    assert "OpenComputer Diagnostic" in arg


def test_slash_registry_contains_debug():
    """Regression: /debug registered in SLASH_REGISTRY."""
    from opencomputer.cli_ui.slash import SLASH_REGISTRY

    names = [c.name for c in SLASH_REGISTRY]
    assert "debug" in names


def test_handlers_dict_contains_debug():
    """Regression: _HANDLERS routes 'debug' to _handle_debug."""
    from opencomputer.cli_ui.slash_handlers import _HANDLERS, _handle_debug

    assert "debug" in _HANDLERS
    assert _HANDLERS["debug"] is _handle_debug
