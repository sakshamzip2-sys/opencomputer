"""PromptBuilder injects personality body into the system prompt."""
from __future__ import annotations

from opencomputer.agent import personality as p_mod
from opencomputer.agent.prompt_builder import PromptBuilder


def _build(builder: PromptBuilder, *, personality: str = "", custom=None) -> str:
    return builder.build(
        personality=personality,
        custom_personalities=dict(custom or {}),
    )


def test_helpful_body_appears_in_system_prompt():
    builder = PromptBuilder()
    prompt = _build(builder, personality="helpful")
    assert p_mod.BUILTINS["helpful"] in prompt


def test_concise_body_appears_when_selected():
    builder = PromptBuilder()
    prompt = _build(builder, personality="concise")
    assert p_mod.BUILTINS["concise"] in prompt
    assert p_mod.BUILTINS["helpful"] not in prompt


def test_unknown_personality_falls_back_to_helpful():
    builder = PromptBuilder()
    prompt = _build(builder, personality="nonexistent_xyz_blah")
    assert p_mod.BUILTINS["helpful"] in prompt


def test_custom_personality_overrides_builtin():
    builder = PromptBuilder()
    prompt = _build(
        builder,
        personality="helpful",
        custom={"helpful": "OVERRIDE-BODY-MARKER-XYZ"},
    )
    assert "OVERRIDE-BODY-MARKER-XYZ" in prompt
    assert p_mod.BUILTINS["helpful"] not in prompt


def test_custom_personality_with_new_name():
    builder = PromptBuilder()
    prompt = _build(
        builder,
        personality="codereviewer",
        custom={"codereviewer": "REVIEWER-BODY-MARKER"},
    )
    assert "REVIEWER-BODY-MARKER" in prompt


def test_personality_section_has_directive_label():
    builder = PromptBuilder()
    prompt = _build(builder, personality="concise")
    assert "Personality directive" in prompt


def test_empty_personality_emits_no_section():
    """Passing empty personality should not emit a personality section.

    (The agent loop currently passes empty string when no personality is
    set, and we want the prompt to omit the section rather than print
    'Personality: ' with nothing after it.)
    """
    builder = PromptBuilder()
    prompt = _build(builder, personality="")
    # When no personality is requested, no body should appear.
    # We verify by checking that the body of "helpful" is NOT in the prompt
    # (it would be if we silently fell back to helpful).
    assert p_mod.BUILTINS["helpful"] not in prompt
    assert "Personality directive" not in prompt
