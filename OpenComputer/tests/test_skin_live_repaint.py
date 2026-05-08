"""D7: Live TUI color repaint on /skin (Hermes v2 production parity).

When the CLI input loop puts its live Rich Console into
``runtime.custom["live_console"]``, the SkinCommand pushes the theme
onto that live console — no session restart needed. Without a live
console (channel adapters, gateway), the command falls back to
throwaway-console + module-state updates so spinner / branding still
hot-swap.
"""
from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console

from opencomputer.agent.slash_commands_impl.skin_personality_cmd import (
    SkinCommand,
    _apply_skin_with_live_console,
)
from plugin_sdk.runtime_context import RuntimeContext


def test_apply_skin_with_no_live_console_returns_false():
    """When runtime.custom has no live console, fall back path returns False."""
    runtime = RuntimeContext()
    used_live = _apply_skin_with_live_console(runtime, "default")
    assert used_live is False


def test_apply_skin_with_live_console_pushes_theme():
    """When a live Console is present, the theme is pushed onto it."""
    buf = StringIO()
    live = Console(file=buf, force_terminal=False, width=80)
    runtime = RuntimeContext()
    runtime.custom["live_console"] = live

    used_live = _apply_skin_with_live_console(runtime, "default")
    assert used_live is True


def test_apply_skin_underscore_key_also_works():
    """Both `live_console` and `_live_console` keys are accepted —
    the underscore form is conventional for plumbing-only keys."""
    buf = StringIO()
    live = Console(file=buf, force_terminal=False, width=80)
    runtime = RuntimeContext()
    runtime.custom["_live_console"] = live

    used_live = _apply_skin_with_live_console(runtime, "default")
    assert used_live is True


@pytest.mark.asyncio
async def test_skin_command_reports_live_repaint_when_console_present(tmp_path, monkeypatch):
    """SkinCommand.execute distinguishes 'live repaint' vs 'next refresh'
    in its result message based on whether a live console was used."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "test")
    (tmp_path / "test").mkdir()

    cmd = SkinCommand()
    runtime = RuntimeContext()
    buf = StringIO()
    runtime.custom["live_console"] = Console(file=buf, force_terminal=False, width=80)

    res = await cmd.execute("ares", runtime)
    assert res.handled
    assert "applied live" in res.output


@pytest.mark.asyncio
async def test_skin_command_reports_deferred_repaint_when_no_console(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "test")
    (tmp_path / "test").mkdir()

    cmd = SkinCommand()
    runtime = RuntimeContext()  # no live_console

    res = await cmd.execute("ares", runtime)
    assert res.handled
    # No live console → message reflects deferred theme repaint.
    assert "next refresh" in res.output


@pytest.mark.asyncio
async def test_skin_reset_with_live_console_reports_live(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "test")
    (tmp_path / "test").mkdir()

    cmd = SkinCommand()
    runtime = RuntimeContext()
    buf = StringIO()
    runtime.custom["live_console"] = Console(file=buf, force_terminal=False, width=80)

    res = await cmd.execute("reset", runtime)
    assert res.handled
    assert "live repaint applied" in res.output


def test_live_console_apply_swallows_theme_failure():
    """Production behavior: if Console.push_theme raises, apply_skin
    catches it, logs a warning, and continues to update module-global
    state. The live-console path returns True because the helper ran
    without raising — the user's response is "live repaint applied"
    even when the theme push silently failed (the warning is in the
    log)."""
    runtime = RuntimeContext()

    class BrokenConsole:
        def push_theme(self, _theme):
            raise RuntimeError("simulated theme push failure")

    runtime.custom["live_console"] = BrokenConsole()

    used_live = _apply_skin_with_live_console(runtime, "default")
    # Helper completed without raising — caller's contract is met.
    assert used_live is True


def test_live_console_load_failure_falls_back():
    """If load_skin itself raises, the fallback path is taken so the
    user still gets a usable module-state update (spinner/branding)."""
    runtime = RuntimeContext()
    buf = StringIO()
    runtime.custom["live_console"] = Console(file=buf, width=80)

    with patch(
        "opencomputer.cli_ui.skin.load_skin",
        side_effect=RuntimeError("simulated load failure"),
    ), patch(
        "opencomputer.agent.slash_commands_impl.skin_personality_cmd."
        "_try_apply_skin_to_module_state"
    ) as mock_fallback:
        used_live = _apply_skin_with_live_console(runtime, "default")
        assert used_live is False
        mock_fallback.assert_called_once_with("default")
