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
