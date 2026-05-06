"""Phase 12 — wizard opt-in step for the social-traces plugin.

Pins the contract that ``_optional_social_traces`` is strictly opt-in
(default no), and that on user confirmation it does the two flips a
manual user would do — ``opencomputer plugin enable social-traces``
(``profile.yaml``) and ``set_enabled(profile_home, True)``
(``state.json``). On user decline it must be a complete no-op.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _patch_setup_wizard_console(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from opencomputer import setup_wizard

    captured: list[str] = []
    monkeypatch.setattr(
        setup_wizard.console,
        "print",
        lambda *args, **_: captured.append(" ".join(str(a) for a in args)),
    )
    return captured


def test_optional_social_traces_default_no_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """User declines (default) → no plugin enable, no state flip, no crash."""
    from opencomputer import setup_wizard

    monkeypatch.setattr(setup_wizard.Confirm, "ask", lambda *_a, **_k: False)
    _patch_setup_wizard_console(monkeypatch)

    fake_cli_plugin = MagicMock()
    monkeypatch.setattr(setup_wizard, "cli_plugin", fake_cli_plugin, raising=False)

    setup_wizard._optional_social_traces()

    fake_cli_plugin.plugin_enable.assert_not_called()


def test_optional_social_traces_yes_flips_both_layers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """User accepts → ``plugin_enable`` AND ``set_enabled(profile_home, True)``."""
    from opencomputer import setup_wizard

    monkeypatch.setattr(setup_wizard.Confirm, "ask", lambda *_a, **_k: True)
    _patch_setup_wizard_console(monkeypatch)

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(profile_home))

    plugin_enable_calls: list[str] = []

    def _fake_plugin_enable(pid: str) -> None:
        plugin_enable_calls.append(pid)

    import opencomputer.cli_plugin as cli_plugin_mod

    monkeypatch.setattr(cli_plugin_mod, "plugin_enable", _fake_plugin_enable)

    setup_wizard._optional_social_traces()

    assert plugin_enable_calls == ["social-traces"]

    # state.json must now report enabled=True
    from opencomputer.cli_traces import _ensure_alias

    _ensure_alias()
    from extensions.social_traces.state import is_enabled  # type: ignore[import-not-found]

    assert is_enabled(profile_home) is True


def test_optional_social_traces_swallows_already_enabled_systemexit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``plugin_enable`` raising typer.Exit (the already-enabled or
    unknown-id path) must NOT abort the state flip — that's how a
    returning user who manually enabled the plugin still gets the
    state.json wired by the wizard.
    """
    from opencomputer import setup_wizard

    monkeypatch.setattr(setup_wizard.Confirm, "ask", lambda *_a, **_k: True)
    _patch_setup_wizard_console(monkeypatch)

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(profile_home))

    def _raise_exit(_pid: str) -> None:
        raise SystemExit(0)

    import opencomputer.cli_plugin as cli_plugin_mod

    monkeypatch.setattr(cli_plugin_mod, "plugin_enable", _raise_exit)

    setup_wizard._optional_social_traces()  # must not propagate SystemExit

    from opencomputer.cli_traces import _ensure_alias

    _ensure_alias()
    from extensions.social_traces.state import is_enabled  # type: ignore[import-not-found]

    assert is_enabled(profile_home) is True
