"""Tests for slash-command aliases (/side → /btw)."""

from __future__ import annotations

from opencomputer.agent.slash_commands import register_builtin_slash_commands
from opencomputer.agent.slash_commands_impl.btw_cmd import BtwCommand
from opencomputer.plugins.registry import registry as plugin_registry
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


def test_btw_command_declares_side_alias():
    cmd = BtwCommand()
    assert "side" in cmd.aliases


def test_aliases_default_to_empty_tuple():
    """Existing commands without aliases keep working."""

    class _NoAlias(SlashCommand):
        name = "noalias"
        description = "test"

        async def execute(self, args, runtime):
            return SlashCommandResult(handled=True, output="ok")

    cmd = _NoAlias()
    assert cmd.aliases == ()


def test_side_resolves_to_btw_in_registry():
    """After register_builtin_slash_commands runs, /side and /btw both
    map to the same BtwCommand instance in the slash_commands dict."""
    register_builtin_slash_commands()
    assert "btw" in plugin_registry.slash_commands
    assert "side" in plugin_registry.slash_commands
    assert plugin_registry.slash_commands["btw"] is plugin_registry.slash_commands["side"]
    assert isinstance(plugin_registry.slash_commands["btw"], BtwCommand)


def test_alias_does_not_overwrite_existing_command():
    """If 'side' is already registered (e.g. by a plugin), the alias
    registration must NOT overwrite it — primary names win, aliases
    yield."""

    # Pre-register a stub under 'side' to simulate a plugin claiming it
    class _SideStub(SlashCommand):
        name = "side"
        description = "stub"

        async def execute(self, args, runtime):
            return SlashCommandResult(handled=True, output="stub")

    plugin_registry.slash_commands.pop("side", None)
    plugin_registry.slash_commands["side"] = _SideStub()
    # Now run idempotent re-registration — alias should yield to existing
    register_builtin_slash_commands()
    assert isinstance(plugin_registry.slash_commands["side"], _SideStub)
    # Cleanup
    plugin_registry.slash_commands.pop("side", None)
