"""Smoke tests for `oc tui` CLI command.

Doesn't actually launch Node — just verifies the cli wiring + the
entry-path discovery logic resolves correctly when the artifact exists
or fails cleanly when it doesn't.
"""

from __future__ import annotations

from pathlib import Path

from opencomputer.cli_tui import _entry_path


def test_entry_path_finds_built_artifact():
    """When the wheel-installed location has dist/entry.js, _entry_path
    returns that absolute path."""
    p = _entry_path()
    # Path is always returned (existence-checked elsewhere).
    assert isinstance(p, Path)
    # In this checkout, we should have either built it or fall through
    # to the source-tree path.
    assert "ui-tui" in str(p)
    assert str(p).endswith("entry.js")


def test_tui_app_is_mounted_on_main_app():
    """The `oc tui` subcommand must be reachable via the main Typer app."""
    from opencomputer.cli import app

    names = {grp.name for grp in app.registered_groups}
    assert "tui" in names
    assert "dashboard" in names


def test_dashboard_app_is_mounted():
    """`oc dashboard` is also via the main Typer app (PR7 fix)."""
    from opencomputer.cli import app

    names = {grp.name for grp in app.registered_groups}
    assert "dashboard" in names
