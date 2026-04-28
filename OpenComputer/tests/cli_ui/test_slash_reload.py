"""Tests for /reload + /reload-mcp slash commands (Hermes Tier 2.A)."""
from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from opencomputer.cli_ui.slash import (
    SLASH_REGISTRY,
    is_slash_command,
    resolve_command,
)
from opencomputer.cli_ui.slash_handlers import (
    SlashContext,
    dispatch_slash,
)


def _make_ctx(*, on_reload=None, on_reload_mcp=None):
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, color_system=None, width=200)
    kwargs = {
        "console": console,
        "session_id": "test",
        "config": None,
        "on_clear": lambda: None,
        "get_cost_summary": lambda: {"in": 0, "out": 0},
        "get_session_list": lambda: [],
    }
    if on_reload is not None:
        kwargs["on_reload"] = on_reload
    if on_reload_mcp is not None:
        kwargs["on_reload_mcp"] = on_reload_mcp
    return SlashContext(**kwargs), buf


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_reload_in_registry():
    cmd = resolve_command("reload")
    assert cmd is not None
    assert "config" in cmd.description.lower() or "env" in cmd.description.lower()


def test_reload_mcp_in_registry():
    cmd = resolve_command("reload-mcp")
    assert cmd is not None
    assert "mcp" in cmd.description.lower()


def test_both_listed_in_registry():
    names = {c.name for c in SLASH_REGISTRY}
    assert "reload" in names
    assert "reload-mcp" in names


# ---------------------------------------------------------------------------
# /reload
# ---------------------------------------------------------------------------


def test_reload_no_changes():
    ctx, buf = _make_ctx(
        on_reload=lambda: {"env_keys_changed": 0, "config_changed": False, "error": None}
    )
    result = dispatch_slash("/reload", ctx)
    assert result.handled
    assert "no changes detected" in buf.getvalue()


def test_reload_env_only():
    ctx, buf = _make_ctx(
        on_reload=lambda: {"env_keys_changed": 3, "config_changed": False, "error": None}
    )
    dispatch_slash("/reload", ctx)
    assert "3 env var(s) updated" in buf.getvalue()


def test_reload_config_only():
    ctx, buf = _make_ctx(
        on_reload=lambda: {"env_keys_changed": 0, "config_changed": True, "error": None}
    )
    dispatch_slash("/reload", ctx)
    assert "config.yaml reloaded" in buf.getvalue()


def test_reload_both():
    ctx, buf = _make_ctx(
        on_reload=lambda: {"env_keys_changed": 2, "config_changed": True, "error": None}
    )
    dispatch_slash("/reload", ctx)
    out = buf.getvalue()
    assert "2 env var(s) updated" in out
    assert "config.yaml reloaded" in out


def test_reload_error():
    ctx, buf = _make_ctx(
        on_reload=lambda: {"env_keys_changed": 0, "config_changed": False, "error": "FileNotFoundError: config.yaml"}
    )
    dispatch_slash("/reload", ctx)
    assert "reload failed" in buf.getvalue().lower()
    assert "FileNotFoundError" in buf.getvalue()


def test_reload_default_callback_warns():
    ctx, buf = _make_ctx()  # default on_reload is dict()
    dispatch_slash("/reload", ctx)
    assert "not wired" in buf.getvalue().lower()


# ---------------------------------------------------------------------------
# /reload-mcp
# ---------------------------------------------------------------------------


def test_reload_mcp_basic():
    ctx, buf = _make_ctx(
        on_reload_mcp=lambda: {
            "servers_before": 2,
            "servers_after": 2,
            "tools_after": 8,
            "error": None,
        }
    )
    result = dispatch_slash("/reload-mcp", ctx)
    assert result.handled
    out = buf.getvalue()
    assert "2 → 2 servers" in out
    assert "8 tool(s)" in out


def test_reload_mcp_no_servers():
    ctx, buf = _make_ctx(
        on_reload_mcp=lambda: {
            "servers_before": 0,
            "servers_after": 0,
            "tools_after": 0,
            "error": None,
        }
    )
    dispatch_slash("/reload-mcp", ctx)
    assert "0 → 0 servers" in buf.getvalue()


def test_reload_mcp_added_servers():
    ctx, buf = _make_ctx(
        on_reload_mcp=lambda: {
            "servers_before": 1,
            "servers_after": 3,
            "tools_after": 12,
            "error": None,
        }
    )
    dispatch_slash("/reload-mcp", ctx)
    assert "1 → 3 servers" in buf.getvalue()


def test_reload_mcp_error():
    ctx, buf = _make_ctx(
        on_reload_mcp=lambda: {
            "servers_before": 0,
            "servers_after": 0,
            "tools_after": 0,
            "error": "ConnectionError: refused",
        }
    )
    dispatch_slash("/reload-mcp", ctx)
    assert "reload-mcp failed" in buf.getvalue().lower()
    assert "ConnectionError" in buf.getvalue()


def test_reload_mcp_default_callback_warns():
    ctx, buf = _make_ctx()  # default on_reload_mcp is dict()
    dispatch_slash("/reload-mcp", ctx)
    assert "not wired" in buf.getvalue().lower()


# ---------------------------------------------------------------------------
# Slash detection
# ---------------------------------------------------------------------------


def test_reload_recognized_as_slash():
    assert is_slash_command("/reload")
    assert is_slash_command("/reload-mcp")
