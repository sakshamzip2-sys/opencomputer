"""Gateway executes slash commands declared ``gateway_safe = True``.

A3 (gateway-vs-CLI parity, Wave 1). Before this fix only commands with
``bypass_running_guard = True`` (just ``/kanban``) ran on the gateway —
every other slash command fell through to the model as plain text. The
``gateway_safe`` flag opts a command into inline gateway execution
without requiring it to also bypass the per-session lock semantics.

The dispatch path under test is ``Dispatch._maybe_bypass_running_guard``,
exercised in isolation with a bare Dispatch shell (same pattern as
``test_goal_midrun_guard.py``).
"""
from __future__ import annotations

import time

import pytest

from opencomputer.gateway import dispatch as disp
from plugin_sdk.core import MessageEvent, Platform
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class _GatewaySafeCmd(SlashCommand):
    name = "fakegwsafe"
    description = "test-only gateway-safe command"
    gateway_safe = True

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        return SlashCommandResult(output=f"ran:{args}")


class _CliOnlyCmd(SlashCommand):
    name = "fakeclionly"
    description = "test-only CLI-only command"
    # gateway_safe defaults to False

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        return SlashCommandResult(output="should-not-run-on-gateway")


def _bare_dispatch() -> disp.Dispatch:
    d = disp.Dispatch.__new__(disp.Dispatch)
    d._active_runs = set()
    d._lifecycle_reactions = False
    return d


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="chat1",
        user_id="user1",
        text=text,
        timestamp=time.time(),
    )


@pytest.fixture
def _register():
    """Register the test commands into the plugin registry, then clean up."""
    from opencomputer.plugins.registry import registry as reg

    safe = _GatewaySafeCmd()
    cli = _CliOnlyCmd()
    reg.slash_commands["fakegwsafe"] = safe
    reg.slash_commands["fakeclionly"] = cli
    try:
        yield
    finally:
        reg.slash_commands.pop("fakegwsafe", None)
        reg.slash_commands.pop("fakeclionly", None)


@pytest.mark.asyncio
async def test_gateway_safe_command_executes(_register):
    d = _bare_dispatch()
    out = await d._maybe_bypass_running_guard(
        _event("/fakegwsafe hello"), "s1", "default",
    )
    assert out == "ran:hello"


@pytest.mark.asyncio
async def test_cli_only_command_falls_through(_register):
    d = _bare_dispatch()
    out = await d._maybe_bypass_running_guard(
        _event("/fakeclionly"), "s1", "default",
    )
    # None → caller proceeds with the normal locked path (text → model).
    assert out is None


@pytest.mark.asyncio
async def test_non_slash_text_falls_through(_register):
    d = _bare_dispatch()
    out = await d._maybe_bypass_running_guard(
        _event("just a normal message"), "s1", "default",
    )
    assert out is None


def test_slash_command_base_defaults_gateway_safe_false():
    assert SlashCommand.gateway_safe is False


def test_audited_builtin_commands_are_tagged_gateway_safe():
    """The read-only informational built-ins audited in A3 must carry the
    flag so they actually answer on Telegram/Discord."""
    from opencomputer.agent.slash_commands_impl.agents_cmd import AgentsCommand
    from opencomputer.agent.slash_commands_impl.capabilities_cmd import (
        CapabilitiesCommand,
    )
    from opencomputer.agent.slash_commands_impl.context_cmd import ContextCommand
    from opencomputer.agent.slash_commands_impl.history_cmd import HistoryCommand
    from opencomputer.agent.slash_commands_impl.platforms_cmd import (
        PlatformsCommand,
    )
    from opencomputer.agent.slash_commands_impl.status_cmd import StatusCommand
    from opencomputer.agent.slash_commands_impl.usage_cmd import UsageCommand

    for cmd_cls in (
        StatusCommand,
        ContextCommand,
        UsageCommand,
        AgentsCommand,
        PlatformsCommand,
        CapabilitiesCommand,
        HistoryCommand,
    ):
        assert cmd_cls.gateway_safe is True, f"{cmd_cls.__name__} not tagged"
