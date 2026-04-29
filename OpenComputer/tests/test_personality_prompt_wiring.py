"""runtime.custom['personality'] shapes the system prompt via base.j2.

PR-5 (2026-04-29) — closes the gap where /personality was a no-op flag.
"""

from __future__ import annotations

from opencomputer.agent.prompt_builder import PromptBuilder


class TestPersonalityPromptWiring:
    def test_default_no_personality_block(self) -> None:
        rendered = PromptBuilder().build()
        assert "Personality directive" not in rendered

    def test_concise_personality_renders_directive(self) -> None:
        rendered = PromptBuilder().build(personality="concise")
        assert "Personality directive" in rendered
        # Should mention terseness / no filler in the directive
        lower = rendered.lower()
        assert (
            "terse" in lower
            or "no filler" in lower
            or "skip preambles" in lower
        )

    def test_technical_personality_renders_directive(self) -> None:
        rendered = PromptBuilder().build(personality="technical")
        assert "Personality directive" in rendered
        assert "technical" in rendered.lower()

    def test_creative_personality_renders_directive(self) -> None:
        rendered = PromptBuilder().build(personality="creative")
        assert "Personality directive" in rendered

    def test_teacher_personality_renders_directive(self) -> None:
        rendered = PromptBuilder().build(personality="teacher")
        assert "Personality directive" in rendered

    def test_hype_personality_renders_directive(self) -> None:
        rendered = PromptBuilder().build(personality="hype")
        assert "Personality directive" in rendered

    def test_helpful_personality_no_overlay(self) -> None:
        # 'helpful' is the baseline — no extra directive needed.
        rendered = PromptBuilder().build(personality="helpful")
        assert "Personality directive" not in rendered

    def test_unknown_personality_no_overlay(self) -> None:
        # Defensive: an unknown personality (typo, future addition) just no-ops.
        rendered = PromptBuilder().build(personality="bogus_unknown")
        assert "Personality directive" not in rendered
