"""Plugin-authored CLI subcommand tests (v1.1 plan-3 M11.5)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

from opencomputer.plugins.loader import PluginAPI


def _make_api() -> PluginAPI:
    """Build a minimal PluginAPI suitable for unit testing."""
    return PluginAPI(
        tool_registry=MagicMock(),
        hook_engine=MagicMock(),
        provider_registry={},
        channel_registry={},
        injection_engine=MagicMock(),
    )


# ─── core registry behavior ────────────────────────────────────────


def test_register_cli_command_with_name() -> None:
    api = _make_api()
    sub = typer.Typer()
    api.register_cli_subcommand(sub, name="myplugin")
    assert api.cli_command_names() == ["myplugin"]
    assert api.get_cli_command("myplugin") is sub


def test_register_cli_command_falls_back_to_plugin_id() -> None:
    api = _make_api()
    api._current_plugin_id = "auto-id-plugin"
    sub = typer.Typer()
    api.register_cli_subcommand(sub)
    assert api.cli_command_names() == ["auto-id-plugin"]


def test_register_cli_command_requires_name_outside_register_context() -> None:
    api = _make_api()
    sub = typer.Typer()
    with pytest.raises(ValueError, match="register_cli_subcommand.*required"):
        api.register_cli_subcommand(sub)


def test_register_cli_command_collision_raises() -> None:
    api = _make_api()
    sub_a = typer.Typer()
    sub_b = typer.Typer()
    api.register_cli_subcommand(sub_a, name="dup")
    with pytest.raises(ValueError, match="dup.*already registered"):
        api.register_cli_subcommand(sub_b, name="dup")


def test_get_cli_command_unknown_raises_keyerror() -> None:
    api = _make_api()
    with pytest.raises(KeyError, match="no CLI command registered"):
        api.get_cli_command("nope")


def test_all_cli_commands_returns_defensive_copy() -> None:
    api = _make_api()
    sub = typer.Typer()
    api.register_cli_subcommand(sub, name="x")
    snapshot = api.all_cli_commands()
    snapshot["evil"] = "tampered"
    # Original registry must be untouched
    assert api.cli_command_names() == ["x"]


def test_register_two_distinct_namespaces() -> None:
    api = _make_api()
    a = typer.Typer()
    b = typer.Typer()
    api.register_cli_subcommand(a, name="alpha")
    api.register_cli_subcommand(b, name="beta")
    assert api.cli_command_names() == ["alpha", "beta"]


# ─── runtime CLI behavior ──────────────────────────────────────────


def test_typer_add_typer_actually_routes_to_plugin_command() -> None:
    """End-to-end: a plugin's typer app, mounted on a parent app, fires
    the plugin's command when invoked via the parent's CLI."""
    parent = typer.Typer()
    plugin_app = typer.Typer()

    captured: dict[str, Any] = {}

    @plugin_app.command()
    def say(word: str) -> None:
        captured["word"] = word
        typer.echo(f"plugin says {word}")

    parent.add_typer(plugin_app, name="myplugin")

    runner = CliRunner()
    result = runner.invoke(parent, ["myplugin", "say", "hello"])
    assert result.exit_code == 0
    assert captured["word"] == "hello"
    assert "plugin says hello" in result.stdout


def test_register_cli_command_supports_register_context_pattern() -> None:
    """Simulates the loader's register(api) flow: set _current_plugin_id
    before register(), the plugin calls register_cli_command without
    a name kwarg, the loader then unsets _current_plugin_id."""
    api = _make_api()

    def plugin_register(api: PluginAPI) -> None:
        sub = typer.Typer()

        @sub.command()
        def hello() -> None:
            pass

        # Mimics a plugin author who omits the ``name=`` kwarg.
        api.register_cli_subcommand(sub)

    # Loader-side scope:
    api._current_plugin_id = "the-plugin-id"
    try:
        plugin_register(api)
    finally:
        api._current_plugin_id = None

    assert api.cli_command_names() == ["the-plugin-id"]
    # And after the loader unsets _current_plugin_id, a subsequent
    # call without name MUST raise — the context is closed.
    sub2 = typer.Typer()
    with pytest.raises(ValueError, match="required"):
        api.register_cli_subcommand(sub2)


def test_two_plugins_register_distinct_namespaces_no_conflict() -> None:
    api = _make_api()

    def plugin_a_register(api: PluginAPI) -> None:
        sub = typer.Typer()
        api.register_cli_subcommand(sub)

    def plugin_b_register(api: PluginAPI) -> None:
        sub = typer.Typer()
        api.register_cli_subcommand(sub)

    api._current_plugin_id = "plugin-a"
    plugin_a_register(api)
    api._current_plugin_id = "plugin-b"
    plugin_b_register(api)
    api._current_plugin_id = None

    assert api.cli_command_names() == ["plugin-a", "plugin-b"]


def test_two_plugins_picking_same_explicit_name_collide() -> None:
    api = _make_api()
    api.register_cli_subcommand(typer.Typer(), name="same")
    with pytest.raises(ValueError, match="already registered"):
        api.register_cli_subcommand(typer.Typer(), name="same")


def test_register_cli_command_accepts_typer_subclass_or_duck_type() -> None:
    """The contract is duck-typed: anything with the typer.Typer shape
    works.  Plugins shouldn't be forced to import typer specifically."""
    api = _make_api()

    class _DuckTyper:
        """Duck-typed stand-in.  The real ``typer.Typer`` is what the
        production CLI mounts via ``add_typer``; the registry doesn't
        care, it just stores the object."""

    duck = _DuckTyper()
    api.register_cli_subcommand(duck, name="duck-plugin")
    assert api.get_cli_command("duck-plugin") is duck
