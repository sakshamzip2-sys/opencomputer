"""Tests for the Hybrid dispatch wrap in the agent loop.

When the slash dispatcher returns a SlashCommandResult with
source='skill', the loop wraps the result as a synthetic SkillTool
tool_use + tool_result message pair — so the model sees skill content
as authoritative tool output (Claude-Code parity) rather than as
plain assistant text.

These tests focus on the message-shape contract:

- skill-source result emits exactly two messages: one assistant with
  tool_calls=[ToolCall(name='Skill', ...)] and one tool result with
  matching tool_call_id and the SKILL body as content.
- command-source result is unchanged: empty list (caller falls
  through to the legacy user/assistant text-reply path).
"""
from __future__ import annotations

from plugin_sdk.slash_command import SlashCommandResult


def test_skill_result_wraps_as_tool_use_pair():
    """source='skill' result generates assistant(tool_calls=Skill) +
    tool(tool_call_id=...) message pair."""
    from opencomputer.agent.loop import _wrap_skill_result_as_tool_messages

    result = SlashCommandResult(
        output="# Skill: hello\n\nBody content here.",
        handled=True,
        source="skill",
    )
    messages = _wrap_skill_result_as_tool_messages(
        skill_name="hello", args="some-args", result=result
    )
    assert len(messages) == 2

    assistant, tool_result = messages
    assert assistant.role == "assistant"
    assert assistant.tool_calls is not None
    assert len(assistant.tool_calls) == 1
    tc = assistant.tool_calls[0]
    assert tc.name == "Skill"
    # Plan §3.4 — name + args land in arguments dict.
    assert tc.arguments.get("name") == "hello"

    assert tool_result.role == "tool"
    assert tool_result.tool_call_id == tc.id
    assert "Body content here." in tool_result.content


def test_command_result_does_not_wrap():
    """source='command' (default) returns empty list — caller emits
    the normal user/assistant pair."""
    from opencomputer.agent.loop import _wrap_skill_result_as_tool_messages

    result = SlashCommandResult(output="hello", handled=True)
    # Default source == "command"; helper must return [] so caller falls
    # through to the existing user/assistant emission.
    messages = _wrap_skill_result_as_tool_messages(
        skill_name="anything", args="", result=result
    )
    assert messages == []


def test_skill_args_passed_into_tool_call_arguments():
    """If the user typed `/foo bar baz`, the args land in the tool_call
    arguments alongside the skill name."""
    from opencomputer.agent.loop import _wrap_skill_result_as_tool_messages

    result = SlashCommandResult(
        output="body",
        handled=True,
        source="skill",
    )
    messages = _wrap_skill_result_as_tool_messages(
        skill_name="my-skill", args="alpha beta", result=result
    )
    tc = messages[0].tool_calls[0]
    assert tc.arguments.get("name") == "my-skill"
    # Args is preserved (downstream may or may not use it).
    assert tc.arguments.get("args") == "alpha beta"


# ─── Task 12.5: full-turn provider integration (audit BLOCKER A1+D3) ──


import pytest
from plugin_sdk.core import Message, ToolCall
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage
from plugin_sdk.tool_contract import ToolSchema


class _RecordingProvider(BaseProvider):
    """Minimal BaseProvider stub: captures messages it receives, returns
    a canned 'ack' assistant reply with end_turn stop reason. Used by
    the Hybrid integration test."""

    name = "recording"
    default_model = "test"

    def __init__(self) -> None:
        self.captured: list[list[Message]] = []

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        stream: bool = False,
        runtime_extras: dict | None = None,
    ) -> ProviderResponse:
        self.captured.append(list(messages))
        return ProviderResponse(
            message=Message(role="assistant", content="ack"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=2),
        )

    async def stream_complete(self, *args, **kwargs):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_hybrid_skill_dispatch_provider_sees_tool_result(tmp_path):
    """Full-turn integration: when the user types `/<skill-name>`, the
    Hybrid wrap fires, the iteration loop continues, and the provider's
    first call receives the synthetic tool_use+tool_result already in
    the messages list."""
    from dataclasses import replace

    from opencomputer.agent.config import default_config
    from opencomputer.agent.loop import AgentLoop

    # Build a memory manager with one skill whose body is "BODY-FOR-TEST".
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    (skills_root / "test-skill").mkdir()
    (skills_root / "test-skill" / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: Test\n---\nBODY-FOR-TEST",
        encoding="utf-8",
    )
    decl = tmp_path / "MEMORY.md"
    decl.write_text("", encoding="utf-8")

    cfg = default_config()
    cfg = replace(
        cfg,
        memory=replace(
            cfg.memory,
            declarative_path=decl,
            skills_path=skills_root,
        ),
        session=replace(cfg.session, db_path=tmp_path / "sessions.db"),
    )

    provider = _RecordingProvider()
    loop = AgentLoop(provider=provider, config=cfg)

    result = await loop.run_conversation("/test-skill", session_id="sess-1")

    # Provider should have been called at least once.
    assert len(provider.captured) >= 1, "provider was never invoked"
    # Inspect the FIRST call's message list — that's what the model saw.
    first_messages = provider.captured[0]
    user_msgs = [m for m in first_messages if m.role == "user" and "/test-skill" in (m.content or "")]
    assistant_with_tool = [
        m for m in first_messages if m.role == "assistant" and m.tool_calls
    ]
    tool_msgs = [m for m in first_messages if m.role == "tool"]

    assert user_msgs, "user message with /test-skill missing from first provider call"
    assert assistant_with_tool, "synthetic assistant tool_call missing"
    assert tool_msgs, "synthetic tool_result missing"

    tc = assistant_with_tool[0].tool_calls[0]
    assert tc.name == "Skill"
    matching_tool = next((m for m in tool_msgs if m.tool_call_id == tc.id), None)
    assert matching_tool is not None
    assert "BODY-FOR-TEST" in matching_tool.content

    # The model's response landed as the conversation's final message.
    assert result.final_message.role == "assistant"
    assert result.final_message.content == "ack"
