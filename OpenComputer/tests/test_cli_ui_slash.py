"""Tests for slash command registry + dispatch."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from opencomputer.cli_ui.slash import (
    CommandDef,
    SLASH_REGISTRY,
    SlashResult,
    is_slash_command,
    resolve_command,
)


def test_is_slash_command_detects_leading_slash():
    assert is_slash_command("/help") is True
    assert is_slash_command("/help arg") is True
    assert is_slash_command(" /help") is False  # must start at col 0
    assert is_slash_command("hello") is False
    assert is_slash_command("") is False
    assert is_slash_command("/") is False  # bare slash is not a command


def test_resolve_command_canonical_name():
    cmd = resolve_command("help")
    assert cmd is not None
    assert cmd.name == "help"


def test_resolve_command_alias():
    cmd = resolve_command("h")
    assert cmd is not None
    assert cmd.name == "help"


def test_resolve_command_with_slash_prefix():
    cmd = resolve_command("/help")
    assert cmd is not None
    assert cmd.name == "help"


def test_resolve_unknown_command_returns_none():
    assert resolve_command("totally-bogus-cmd") is None


def test_registry_has_required_commands():
    names = {cmd.name for cmd in SLASH_REGISTRY}
    assert {
        "exit",
        "clear",
        "help",
        "screenshot",
        "export",
        "cost",
        "model",
        "sessions",
    } <= names


def test_slash_result_dataclass_shape():
    r = SlashResult(handled=True, exit_loop=False, message="ok")
    assert r.handled is True
    assert r.exit_loop is False
    assert r.message == "ok"


def test_command_def_has_aliases_and_args_hint():
    cmd = resolve_command("help")
    assert isinstance(cmd, CommandDef)
    assert "h" in cmd.aliases
    # args_hint may be empty string but must be defined
    assert cmd.args_hint == ""


# ---------- Handler dispatch tests (Task 4) ----------

from opencomputer.cli_ui.slash_handlers import SlashContext, dispatch_slash


def _make_ctx(console: Console | None = None) -> SlashContext:
    return SlashContext(
        console=console or Console(record=True),
        session_id="test-session",
        config=MagicMock(model=MagicMock(model="claude-3-5", provider="anthropic")),
        on_clear=lambda: None,
        get_cost_summary=lambda: {"in": 100, "out": 200},
        get_session_list=lambda: [{"id": "s1", "started_at": "2026-01-01T00:00:00"}],
    )


def test_dispatch_unknown_returns_unhandled_or_error():
    """Unknown slash command is consumed (not sent to LLM) and prints an error."""
    console = Console(record=True)
    ctx = _make_ctx(console=console)
    r = dispatch_slash("/totally-bogus", ctx)
    # Per plan: unknown commands ARE handled (we ate them, don't send to LLM)
    # but get an error message. handled=True, exit_loop=False.
    assert r.handled is True
    out = console.export_text()
    assert "unknown" in out.lower()


def test_dispatch_non_slash_returns_unhandled():
    ctx = _make_ctx()
    r = dispatch_slash("hello world", ctx)
    assert r.handled is False


def test_dispatch_exit_signals_loop_exit():
    ctx = _make_ctx()
    r = dispatch_slash("/exit", ctx)
    assert r.handled is True
    assert r.exit_loop is True


def test_dispatch_help_lists_commands():
    console = Console(record=True)
    ctx = _make_ctx(console=console)
    r = dispatch_slash("/help", ctx)
    assert r.handled is True
    assert r.exit_loop is False
    out = console.export_text()
    assert "/exit" in out
    assert "/help" in out


def test_dispatch_clear_calls_callback():
    called: list[bool] = []
    ctx = _make_ctx()
    ctx.on_clear = lambda: called.append(True)
    r = dispatch_slash("/clear", ctx)
    assert r.handled is True
    assert called == [True]


def test_dispatch_screenshot_writes_file():
    console = Console(record=True)
    console.print("hello world")
    ctx = _make_ctx(console=console)
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "snap.txt"
        r = dispatch_slash(f"/screenshot {out_path}", ctx)
        assert r.handled is True
        assert out_path.exists()
        assert "hello world" in out_path.read_text()


def test_dispatch_export_writes_file():
    console = Console(record=True)
    console.print("turn 1")
    ctx = _make_ctx(console=console)
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "transcript.md"
        r = dispatch_slash(f"/export {out_path}", ctx)
        assert r.handled is True
        assert out_path.exists()
        text = out_path.read_text()
        assert "turn 1" in text


def test_dispatch_cost_prints_summary():
    console = Console(record=True)
    ctx = _make_ctx(console=console)
    ctx.get_cost_summary = lambda: {"in": 1234, "out": 5678}
    r = dispatch_slash("/cost", ctx)
    assert r.handled is True
    out = console.export_text()
    assert "1234" in out
    assert "5678" in out


def test_dispatch_model_prints_active_model():
    console = Console(record=True)
    ctx = _make_ctx(console=console)
    r = dispatch_slash("/model", ctx)
    assert r.handled is True
    out = console.export_text()
    assert "claude-3-5" in out
    assert "anthropic" in out


def test_dispatch_sessions_lists_session_ids():
    console = Console(record=True)
    ctx = _make_ctx(console=console)
    r = dispatch_slash("/sessions", ctx)
    assert r.handled is True
    out = console.export_text()
    assert "s1" in out
    assert "2026-01-01" in out


def test_dispatch_alias_resolves():
    ctx = _make_ctx()
    r = dispatch_slash("/q", ctx)  # alias for /exit
    assert r.handled is True
    assert r.exit_loop is True
