"""v1.1 plan-4 M13 — plugin-authored top-level CLI subcommand registration.

Pins the contract:

* ``PluginAPI.register_cli_command(name, app, *, replace=False)`` accepts
  a Typer app and stores it in the shared ``_cli_commands`` table.
* Core verb collisions raise :class:`PluginCLINameCollision` regardless
  of ``replace`` (cross-tested in ``test_plugin_cli_core_collision.py``).
* Plugin-vs-plugin collisions raise :class:`PluginCLINameCollision`
  unless ``replace=True``.
* The lazy placeholder built by ``cli_registry._make_lazy_plugin_cli``
  loads the owning plugin on first invocation and re-dispatches into
  the plugin's real Typer app with the recovered argv.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from opencomputer.plugins.loader import PluginAPI, PluginCLINameCollision


def _make_multi_root() -> typer.Typer:
    """Build a root Typer with at least 2 commands so typer treats it
    as a group (single-command Typer apps auto-promote and break our
    `oc <name> <subcommand>` dispatch)."""
    root = typer.Typer()

    @root.command("__ping")
    def _ping() -> None:
        typer.echo("pong")

    return root


def _bare_api() -> PluginAPI:
    """Return a PluginAPI with no real registries — enough for register_cli_command."""
    return PluginAPI(
        tool_registry=None,  # type: ignore[arg-type]
        hook_engine=None,
        provider_registry={},
        channel_registry={},
    )


# ─── register_cli_command happy path ──────────────────────────────────────


def test_register_cli_command_stores_app() -> None:
    api = _bare_api()
    sub = typer.Typer()

    @sub.command("hello")
    def _hello() -> None:
        typer.echo("world")

    api.register_cli_command("fake", sub)
    assert "fake" in api._cli_commands
    assert api._cli_commands["fake"] is sub


def test_register_cli_command_two_distinct_names_coexist() -> None:
    api = _bare_api()
    a, b = typer.Typer(), typer.Typer()
    api.register_cli_command("alpha", a)
    api.register_cli_command("beta", b)
    assert api._cli_commands["alpha"] is a
    assert api._cli_commands["beta"] is b


# ─── plugin-vs-plugin collision ──────────────────────────────────────────


def test_register_cli_command_duplicate_raises() -> None:
    api = _bare_api()
    api.register_cli_command("dup", typer.Typer())
    with pytest.raises(PluginCLINameCollision) as exc_info:
        api.register_cli_command("dup", typer.Typer())
    assert "already registered" in str(exc_info.value)


def test_register_cli_command_replace_true_overrides() -> None:
    api = _bare_api()
    first = typer.Typer()
    second = typer.Typer()
    api.register_cli_command("override", first)
    api.register_cli_command("override", second, replace=True)
    assert api._cli_commands["override"] is second


# ─── lazy placeholder dispatch ────────────────────────────────────────────


def test_lazy_placeholder_dispatches_to_real_typer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the M13 lazy-load: placeholder fires register(api), then forwards argv."""
    from opencomputer.plugins.cli_registry import _attach_lazy_command
    from opencomputer.plugins.registry import registry as _registry

    # Build a real Typer the plugin would have registered. Two commands
    # (greet + noop) force typer to treat the app as a group.
    real = typer.Typer(no_args_is_help=False)
    captured: dict[str, str] = {}

    @real.command("greet")
    def _greet(name: str = typer.Argument("world")) -> None:
        captured["name"] = name
        typer.echo(f"hello {name}")

    @real.command("noop")
    def _noop() -> None:
        typer.echo("noop")

    # Pre-populate the registry as if the plugin had already loaded.
    _registry.cli_commands.pop("fake-cmd", None)
    _registry.cli_commands["fake-cmd"] = real
    _registry.shared_api = _registry.api()

    root = _make_multi_root()
    _attach_lazy_command(
        root,
        plugin_id="fake-plugin",
        plugin_name="Fake Plugin",
        command_name="fake-cmd",
    )

    runner = CliRunner()
    result = runner.invoke(root, ["fake-cmd", "greet", "Saksham"])
    # Cleanup so other tests don't see leakage
    _registry.cli_commands.pop("fake-cmd", None)
    assert result.exit_code == 0, result.output
    assert captured.get("name") == "Saksham", f"output: {result.output!r}"


def test_lazy_placeholder_errors_when_plugin_did_not_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a plugin advertises cli_commands but never calls register_cli_command,
    the placeholder must error out with a clear message rather than silent-fail."""
    from opencomputer.plugins.cli_registry import _attach_lazy_command
    from opencomputer.plugins.registry import registry as _registry

    # Stub out discovery so the lazy load doesn't try to import any real plugin
    monkeypatch.setattr(
        "opencomputer.plugins.discovery.discover", lambda paths: []
    )

    # Ensure there's no stale registration from another test
    _registry.cli_commands.pop("ghost", None)
    _registry.shared_api = _registry.api()

    root = _make_multi_root()
    _attach_lazy_command(
        root,
        plugin_id="ghost-plugin",
        plugin_name="Ghost",
        command_name="ghost",
    )

    runner = CliRunner()
    result = runner.invoke(root, ["ghost", "anything"])
    assert result.exit_code != 0
    assert "did not call" in result.output, f"missing error message; output:\n{result.output}"


# ─── manifest schema acceptance ──────────────────────────────────────────


def test_manifest_schema_accepts_cli_commands() -> None:
    """Plain `cli_commands: ["fake"]` must validate under the v5 schema."""
    from opencomputer.plugins.manifest_validator import (
        PluginManifestSchema,
    )

    schema = PluginManifestSchema(
        id="fake",
        name="Fake",
        version="1.0.0",
        entry="plugin",
        cli_commands=["fake-cmd"],
    )
    assert schema.cli_commands == ["fake-cmd"]


def test_manifest_schema_rejects_dash_prefix() -> None:
    """`cli_commands: ["--evil"]` must be rejected — would alias a flag."""
    from opencomputer.plugins.manifest_validator import (
        PluginManifestSchema,
    )

    with pytest.raises(Exception):  # pydantic ValidationError
        PluginManifestSchema(
            id="evil",
            name="Evil",
            version="1.0.0",
            entry="plugin",
            cli_commands=["--evil"],
        )


def test_manifest_schema_rejects_empty_cli_commands_entry() -> None:
    from opencomputer.plugins.manifest_validator import (
        PluginManifestSchema,
    )

    with pytest.raises(Exception):
        PluginManifestSchema(
            id="oops",
            name="Oops",
            version="1.0.0",
            entry="plugin",
            cli_commands=[""],
        )
