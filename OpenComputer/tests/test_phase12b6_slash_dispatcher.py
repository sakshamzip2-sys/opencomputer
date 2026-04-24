"""Phase 12b.6 — Slash-command router formalization (Sub-project D, Task D8).

Formalizes the Phase-6f duck-typed slash-command contract into a proper
``plugin_sdk.SlashCommand`` ABC + ``SlashCommandResult`` dataclass +
``opencomputer.agent.slash_dispatcher`` module that the AgentLoop calls
before invoking the LLM.

Two concerns tested:

1. Pure parser + dispatcher unit behavior (parse_slash, dispatch),
   including legacy duck-typed compat and exception-swallowing.
2. AgentLoop integration — when a user's message matches a registered
   slash command, the agent loop returns early WITHOUT calling
   provider.complete / stream_complete.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import Message
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

# ─── 1. parse_slash unit tests ────────────────────────────────────────


def test_parse_slash_extracts_name_and_args() -> None:
    from opencomputer.agent.slash_dispatcher import parse_slash

    assert parse_slash("/plan do the thing") == ("plan", "do the thing")


def test_parse_slash_bare_command_has_empty_args() -> None:
    from opencomputer.agent.slash_dispatcher import parse_slash

    assert parse_slash("/plan") == ("plan", "")


def test_parse_slash_not_a_slash_returns_none() -> None:
    from opencomputer.agent.slash_dispatcher import parse_slash

    assert parse_slash("hello") is None


# ─── 2. dispatch behavior ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_unknown_command_returns_none() -> None:
    from opencomputer.agent.slash_dispatcher import dispatch

    result = await dispatch("/foo", {}, DEFAULT_RUNTIME_CONTEXT)
    assert result is None


@pytest.mark.asyncio
async def test_dispatch_calls_registered_command() -> None:
    """A SlashCommand subclass's execute runs with the right args."""
    from opencomputer.agent.slash_dispatcher import dispatch

    class FakeCommand(SlashCommand):
        name = "fake"
        description = "fake command for testing"

        def __init__(self) -> None:
            self.received_args: str | None = None

        async def execute(
            self, args: str, runtime: Any
        ) -> SlashCommandResult:
            self.received_args = args
            return SlashCommandResult(output="ok", handled=True)

    cmd = FakeCommand()
    result = await dispatch(
        "/fake some args here", {"fake": cmd}, DEFAULT_RUNTIME_CONTEXT
    )
    assert isinstance(result, SlashCommandResult)
    assert result.output == "ok"
    assert result.handled is True
    assert cmd.received_args == "some args here"


@pytest.mark.asyncio
async def test_dispatch_wraps_bare_string_return() -> None:
    """Legacy duck-typed commands may return a plain string — wrap it."""
    from opencomputer.agent.slash_dispatcher import dispatch

    class LegacyDuckTyped:
        name = "legacy"
        description = "legacy duck-typed command"

        async def execute(self, args: str, runtime: Any) -> str:
            return "plain-string-result"

    result = await dispatch(
        "/legacy", {"legacy": LegacyDuckTyped()}, DEFAULT_RUNTIME_CONTEXT
    )
    assert isinstance(result, SlashCommandResult)
    assert result.output == "plain-string-result"
    assert result.handled is True


@pytest.mark.asyncio
async def test_dispatch_swallows_command_exceptions() -> None:
    """If execute raises, dispatcher returns a handled SlashCommandResult
    describing the failure — never propagates the exception.
    """
    from opencomputer.agent.slash_dispatcher import dispatch

    class BrokenCommand:
        name = "boom"
        description = "a broken command"

        async def execute(self, args: str, runtime: Any) -> str:
            raise RuntimeError("kaboom")

    result = await dispatch(
        "/boom", {"boom": BrokenCommand()}, DEFAULT_RUNTIME_CONTEXT
    )
    assert isinstance(result, SlashCommandResult)
    assert result.handled is True
    assert "boom" in result.output
    assert "RuntimeError" in result.output
    assert "kaboom" in result.output


# ─── 3. AgentLoop integration ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_loop_slash_command_returns_early_without_llm(tmp_path) -> None:
    """When the user types a registered slash command, the agent loop
    returns its output without touching provider.complete / stream_complete.
    """
    from opencomputer.agent.config import (
        Config,
        LoopConfig,
        MemoryConfig,
        ModelConfig,
        SessionConfig,
    )
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.state import SessionDB
    from opencomputer.plugins.registry import registry as plugin_registry

    # Mock provider — fail loudly if anyone calls complete/stream.
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=AssertionError("provider.complete must NOT be called"))
    provider.stream_complete = AsyncMock(side_effect=AssertionError("provider.stream_complete must NOT be called"))

    # Tiny in-memory-ish config pinned to tmp_path so we don't pollute
    # the real profile.
    db_path = tmp_path / "sessions.db"
    cfg = Config(
        model=ModelConfig(provider="anthropic", model="claude-3-opus"),
        loop=LoopConfig(max_iterations=3),
        session=SessionConfig(db_path=db_path),
        memory=MemoryConfig(
            declarative_path=tmp_path / "MEMORY.md",
            skills_path=tmp_path / "skills",
            user_path=tmp_path / "USER.md",
            soul_path=tmp_path / "SOUL.md",
        ),
    )

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        db=SessionDB(db_path),
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )

    # Register a fake slash command on the shared registry.
    class FakeSlashCommand(SlashCommand):
        name = "testcmd"
        description = "test command"

        async def execute(
            self, args: str, runtime: Any
        ) -> SlashCommandResult:
            return SlashCommandResult(
                output=f"fake output with args={args!r}", handled=True
            )

    saved = plugin_registry.slash_commands.copy()
    plugin_registry.slash_commands["testcmd"] = FakeSlashCommand()
    try:
        result = await loop.run_conversation("/testcmd hello world")
    finally:
        plugin_registry.slash_commands.clear()
        plugin_registry.slash_commands.update(saved)

    # Provider was NEVER called.
    provider.complete.assert_not_called()
    provider.stream_complete.assert_not_called()

    # Result reflects the command's output.
    assert isinstance(result.final_message, Message)
    assert result.final_message.role == "assistant"
    assert "fake output" in (result.final_message.content or "")
    assert "hello world" in (result.final_message.content or "")
    assert result.iterations == 0
    assert result.input_tokens == 0
    assert result.output_tokens == 0
