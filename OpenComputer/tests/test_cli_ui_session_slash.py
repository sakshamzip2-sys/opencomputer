"""Tests for /rename and /resume slash commands (Phase 2.A)."""
from __future__ import annotations

from unittest.mock import MagicMock

from rich.console import Console

from opencomputer.cli_ui.slash import SLASH_REGISTRY, resolve_command
from opencomputer.cli_ui.slash_handlers import SlashContext, dispatch_slash


def _make_ctx(
    console=None,
    on_rename=None,
    on_resume=None,
):
    return SlashContext(
        console=console or Console(record=True),
        session_id="sess-123",
        config=MagicMock(model=MagicMock(model="m", provider="p")),
        on_clear=lambda: None,
        get_cost_summary=lambda: {"in": 0, "out": 0},
        get_session_list=lambda: [],
        on_rename=on_rename or (lambda title: True),
        on_resume=on_resume or (lambda target: True),
    )


def test_registry_has_rename_and_resume():
    names = {cmd.name for cmd in SLASH_REGISTRY}
    assert "rename" in names
    assert "resume" in names


def test_resolve_rename_alias():
    cmd = resolve_command("title")
    assert cmd is not None
    assert cmd.name == "rename"


def test_resolve_resume():
    cmd = resolve_command("resume")
    assert cmd is not None
    assert cmd.name == "resume"


def test_dispatch_rename_calls_callback():
    captured: list[str] = []
    ctx = _make_ctx(on_rename=lambda title: captured.append(title) or True)
    r = dispatch_slash("/rename my project debug", ctx)
    assert r.handled is True
    assert captured == ["my project debug"]


def test_dispatch_rename_empty_title_errors_no_callback():
    captured: list[str] = []
    console = Console(record=True)
    ctx = _make_ctx(console=console, on_rename=lambda t: captured.append(t) or True)
    r = dispatch_slash("/rename", ctx)
    assert r.handled is True
    assert captured == []
    # Error message mentions title
    out = console.export_text().lower()
    assert "title" in out


def test_dispatch_rename_callback_failure_prints_error():
    console = Console(record=True)
    ctx = _make_ctx(console=console, on_rename=lambda t: False)
    r = dispatch_slash("/rename foo", ctx)
    assert r.handled is True
    assert "fail" in console.export_text().lower()


def test_dispatch_rename_alias_title():
    captured: list[str] = []
    ctx = _make_ctx(on_rename=lambda title: captured.append(title) or True)
    r = dispatch_slash("/title hello world", ctx)
    assert r.handled is True
    assert captured == ["hello world"]


def test_dispatch_resume_no_args_means_pick():
    captured: list[str] = []
    ctx = _make_ctx(on_resume=lambda target: captured.append(target) or True)
    r = dispatch_slash("/resume", ctx)
    assert r.handled is True
    assert captured == ["pick"]


def test_dispatch_resume_with_last():
    captured: list[str] = []
    ctx = _make_ctx(on_resume=lambda target: captured.append(target) or True)
    r = dispatch_slash("/resume last", ctx)
    assert r.handled is True
    assert captured == ["last"]


def test_dispatch_resume_with_id_prefix():
    captured: list[str] = []
    ctx = _make_ctx(on_resume=lambda target: captured.append(target) or True)
    r = dispatch_slash("/resume abc123", ctx)
    assert r.handled is True
    assert captured == ["abc123"]


def test_dispatch_resume_callback_failure_prints_error():
    console = Console(record=True)
    ctx = _make_ctx(console=console, on_resume=lambda target: False)
    r = dispatch_slash("/resume nonexistent", ctx)
    assert r.handled is True
    assert "fail" in console.export_text().lower()
