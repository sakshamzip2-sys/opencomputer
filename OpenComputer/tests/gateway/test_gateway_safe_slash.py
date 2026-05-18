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


class _CaptureRuntimeCmd(SlashCommand):
    """Echoes the runtime.custom keys it was handed — used to prove the
    dispatcher plumbs loop-backed context (session_db / model)."""

    name = "fakecapture"
    description = "test-only runtime-capturing command"
    gateway_safe = True
    seen: dict | None = None

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        type(self).seen = dict(runtime.custom or {})
        return SlashCommandResult(output="captured")


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
    """The built-ins that actually function with the runtime the gateway
    builds must carry the flag so they answer on Telegram/Discord.

    /status, /history, /agents — work once the dispatcher plumbs
    session_db/model into runtime.custom. /capabilities self-imports the
    plugin registry, so it never needed runtime context.
    """
    from opencomputer.agent.slash_commands_impl.agents_cmd import AgentsCommand
    from opencomputer.agent.slash_commands_impl.capabilities_cmd import (
        CapabilitiesCommand,
    )
    from opencomputer.agent.slash_commands_impl.history_cmd import HistoryCommand
    from opencomputer.agent.slash_commands_impl.status_cmd import StatusCommand

    for cmd_cls in (
        StatusCommand,
        AgentsCommand,
        CapabilitiesCommand,
        HistoryCommand,
    ):
        assert cmd_cls.gateway_safe is True, f"{cmd_cls.__name__} not tagged"


def test_counter_dependent_commands_are_not_gateway_safe():
    """/context, /usage, /platforms read live counters / adapter roster
    the gateway bypass path cannot supply — tagging them would ship a
    command that shows misleading zeros. /handoff needs persist-then-
    inject machinery (A8 deferred). They must stay untagged."""
    from opencomputer.agent.slash_commands_impl.context_cmd import ContextCommand
    from opencomputer.agent.slash_commands_impl.handoff_cmd import HandoffCommand
    from opencomputer.agent.slash_commands_impl.platforms_cmd import (
        PlatformsCommand,
    )
    from opencomputer.agent.slash_commands_impl.usage_cmd import UsageCommand

    for cmd_cls in (ContextCommand, UsageCommand, PlatformsCommand, HandoffCommand):
        assert cmd_cls.gateway_safe is False, (
            f"{cmd_cls.__name__} is tagged but cannot work on the gateway"
        )


class _FakeLoop:
    """Minimal AgentLoop stand-in exposing what the dispatcher reads."""

    def __init__(self) -> None:
        self.db = object()  # sentinel — only identity is checked

        class _M:
            model = "claude-opus-4-7"

        class _Cfg:
            model = _M()

        self.config = _Cfg()


@pytest.mark.asyncio
async def test_loop_context_is_plumbed_into_runtime():
    """A3 fix — with a loop, the runtime handed to a gateway-safe command
    carries session_db / model / active_profile_id so read-only commands
    show real data instead of placeholders."""
    from opencomputer.plugins.registry import registry as reg

    cmd = _CaptureRuntimeCmd()
    _CaptureRuntimeCmd.seen = None
    reg.slash_commands["fakecapture"] = cmd
    loop = _FakeLoop()
    try:
        d = _bare_dispatch()
        out = await d._maybe_bypass_running_guard(
            _event("/fakecapture"), "sess-9", "stocks", loop,
        )
    finally:
        reg.slash_commands.pop("fakecapture", None)

    assert out == "captured"
    seen = _CaptureRuntimeCmd.seen
    assert seen is not None
    assert seen["session_db"] is loop.db
    assert seen["model"] == "claude-opus-4-7"
    assert seen["active_profile_id"] == "stocks"


@pytest.mark.asyncio
async def test_no_loop_still_dispatches_without_loop_keys():
    """Backwards compat — the 3-arg call (no loop) still works; loop-only
    keys are simply absent."""
    from opencomputer.plugins.registry import registry as reg

    cmd = _CaptureRuntimeCmd()
    _CaptureRuntimeCmd.seen = None
    reg.slash_commands["fakecapture"] = cmd
    try:
        d = _bare_dispatch()
        out = await d._maybe_bypass_running_guard(
            _event("/fakecapture"), "sess-9", "default",
        )
    finally:
        reg.slash_commands.pop("fakecapture", None)

    assert out == "captured"
    assert _CaptureRuntimeCmd.seen is not None
    assert "session_db" not in _CaptureRuntimeCmd.seen
    assert _CaptureRuntimeCmd.seen["active_profile_id"] == "default"
