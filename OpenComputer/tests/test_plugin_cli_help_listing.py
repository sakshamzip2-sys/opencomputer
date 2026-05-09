"""v1.1 plan-4 M13 — `oc --help` lists plugin-advertised CLI commands
without importing any plugin's Python.

Approach: stub `discover()` to return a fake candidate with a
`cli_commands` manifest entry. Verify the placeholder is attached
and visible in `--help`, that the plugin module never gets loaded
during help, and that actually invoking the command DOES load the
plugin and dispatch into it.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner


def _fake_candidate(plugin_id: str, command_name: str):
    """Build a minimal PluginCandidate look-alike.

    The real :class:`PluginCandidate` is a dataclass with a `manifest`
    field. Our stubs only need `manifest.id`, `manifest.name`,
    `manifest.cli_commands`, and `manifest.cli_commands_profiles`.
    """
    from types import SimpleNamespace

    manifest = SimpleNamespace(
        id=plugin_id,
        name=plugin_id.replace("-", " ").title(),
        cli_commands=(command_name,),
        cli_commands_profiles=None,
    )
    return SimpleNamespace(manifest=manifest)


def test_help_lists_advertised_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--help` on the root must show the plugin-advertised name."""
    from opencomputer.plugins import cli_registry

    monkeypatch.setattr(
        cli_registry,
        "discover",
        lambda paths: [_fake_candidate("fake-cli", "fake-hi")],
    )
    monkeypatch.setattr(
        cli_registry, "standard_search_paths", lambda: [], raising=False
    )
    monkeypatch.setattr(
        cli_registry, "read_active_profile", lambda: "default", raising=False
    )

    root = typer.Typer()

    @root.command("__ping")
    def _ping() -> None:
        typer.echo("pong")

    cli_registry.register_plugin_cli_commands(root)

    runner = CliRunner()
    result = runner.invoke(root, ["--help"])
    assert result.exit_code == 0, result.output
    assert "fake-hi" in result.output, f"--help missing fake-hi:\n{result.output}"


def test_help_does_not_import_plugin_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lazy contract: discovery must not import plugin.py.

    Verified by tracking calls to ``load_plugin``: it should be called
    ZERO times during ``--help`` and ONCE on actual invocation.
    """
    from opencomputer.plugins import cli_registry

    monkeypatch.setattr(
        cli_registry,
        "discover",
        lambda paths: [_fake_candidate("fake-cli", "fake-hi")],
    )
    monkeypatch.setattr(
        cli_registry, "standard_search_paths", lambda: [], raising=False
    )
    monkeypatch.setattr(
        cli_registry, "read_active_profile", lambda: "default", raising=False
    )

    load_calls: list[str] = []

    def _spy_load_plugin(cand, api, **kwargs):
        load_calls.append(cand.manifest.id)
        return None

    # Patch the loader at its lazy-dispatch import site.
    import opencomputer.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod, "load_plugin", _spy_load_plugin)

    root = typer.Typer()

    @root.command("__ping")
    def _ping() -> None:
        typer.echo("pong")

    cli_registry.register_plugin_cli_commands(root)

    runner = CliRunner()
    result = runner.invoke(root, ["--help"])
    assert result.exit_code == 0, result.output
    assert load_calls == [], (
        f"plugin loaded during --help (laziness broken): {load_calls}"
    )


def test_invocation_triggers_plugin_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """Actually invoking `oc fake-hi ...` must trigger lazy load AND
    dispatch into the plugin's registered Typer."""
    from opencomputer.plugins import cli_registry
    from opencomputer.plugins.registry import registry as _registry

    # Pre-populate the cli_commands table so the lazy-dispatch's load
    # step finds the registration without us having to spin up a real
    # plugin module.
    real = typer.Typer(no_args_is_help=False)
    captured: dict[str, str] = {}

    @real.command("greet")
    def _greet(name: str = typer.Argument("world")) -> None:
        captured["name"] = name
        typer.echo(f"hi {name}")

    @real.command("noop")  # 2nd cmd forces group behavior
    def _noop() -> None:
        typer.echo("noop")

    _registry.cli_commands.pop("fake-hi", None)
    _registry.cli_commands["fake-hi"] = real
    _registry.shared_api = _registry.api()

    monkeypatch.setattr(
        cli_registry,
        "discover",
        lambda paths: [_fake_candidate("fake-cli", "fake-hi")],
    )
    monkeypatch.setattr(
        cli_registry, "standard_search_paths", lambda: [], raising=False
    )
    monkeypatch.setattr(
        cli_registry, "read_active_profile", lambda: "default", raising=False
    )

    root = typer.Typer()

    @root.command("__ping")
    def _ping() -> None:
        typer.echo("pong")

    cli_registry.register_plugin_cli_commands(root)

    runner = CliRunner()
    result = runner.invoke(root, ["fake-hi", "greet", "Saksham"])
    _registry.cli_commands.pop("fake-hi", None)
    assert result.exit_code == 0, result.output
    assert captured.get("name") == "Saksham", f"output: {result.output}"


def test_profile_scoped_command_skipped_when_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `cli_commands_profiles: ["work"]` manifest is hidden under `default`."""
    from types import SimpleNamespace

    from opencomputer.plugins import cli_registry

    cand = SimpleNamespace(
        manifest=SimpleNamespace(
            id="work-only",
            name="Work Only",
            cli_commands=("work-only",),
            cli_commands_profiles=("work",),
        )
    )

    monkeypatch.setattr(cli_registry, "discover", lambda paths: [cand])
    monkeypatch.setattr(
        cli_registry, "standard_search_paths", lambda: [], raising=False
    )
    monkeypatch.setattr(
        cli_registry, "read_active_profile", lambda: "default", raising=False
    )

    root = typer.Typer()

    @root.command("__ping")
    def _ping() -> None:
        typer.echo("pong")

    cli_registry.register_plugin_cli_commands(root)

    runner = CliRunner()
    result = runner.invoke(root, ["--help"])
    assert result.exit_code == 0, result.output
    assert "work-only" not in result.output


def test_profile_scoped_command_visible_when_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same manifest but the active profile matches → command is exposed."""
    from types import SimpleNamespace

    from opencomputer.plugins import cli_registry

    cand = SimpleNamespace(
        manifest=SimpleNamespace(
            id="work-only",
            name="Work Only",
            cli_commands=("work-only",),
            cli_commands_profiles=("work",),
        )
    )

    monkeypatch.setattr(cli_registry, "discover", lambda paths: [cand])
    monkeypatch.setattr(
        cli_registry, "standard_search_paths", lambda: [], raising=False
    )
    monkeypatch.setattr(
        cli_registry, "read_active_profile", lambda: "work", raising=False
    )

    root = typer.Typer()

    @root.command("__ping")
    def _ping() -> None:
        typer.echo("pong")

    cli_registry.register_plugin_cli_commands(root)

    runner = CliRunner()
    result = runner.invoke(root, ["--help"])
    assert result.exit_code == 0, result.output
    assert "work-only" in result.output
