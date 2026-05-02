"""The base.j2 system prompt must include the <use_parallel_tool_calls> nudge.

Item 4 (2026-05-02): canonical wording from Anthropic tool-use docs that the
model is trained to give extra weight when wrapped in this XML-like form.
"""
from opencomputer.agent.prompt_builder import PromptBuilder


def test_parallel_nudge_present_in_default_persona():
    builder = PromptBuilder()
    rendered = builder.build(active_persona_id="")
    assert "<use_parallel_tool_calls>" in rendered
    assert "invoke all relevant tools simultaneously" in rendered
    assert "</use_parallel_tool_calls>" in rendered


def test_parallel_nudge_present_in_companion_persona():
    builder = PromptBuilder()
    rendered = builder.build(active_persona_id="companion")
    assert "<use_parallel_tool_calls>" in rendered
