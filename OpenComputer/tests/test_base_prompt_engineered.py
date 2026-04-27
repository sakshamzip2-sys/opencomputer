"""V3.A-T3 — engineered base.j2 snapshot tests.

The 47-line stub was rewritten to a multi-section engineered prompt that
mirrors Claude Code's structure (identity, system info, working rules,
tool-use discipline, plan mode, yolo mode, memory integration, skills,
error recovery, refusal policy, wrap-up). These tests guard the rewrite:
required sections exist, slot conditionals fire, line count grew, and
the legacy memory/profile/soul slots still render.

If any of these break, the rewrite drifted — fix the prompt, don't
soften the assertion.
"""

from __future__ import annotations

from pathlib import Path

from opencomputer.agent.prompt_builder import PromptBuilder


def test_base_prompt_contains_required_sections() -> None:
    """Every engineered section header must render in the default build."""
    pb = PromptBuilder()
    rendered = pb.build()
    # Identity + system info
    assert "OpenComputer" in rendered
    assert "# System info" in rendered
    assert "Working directory:" in rendered
    assert "Operating system:" in rendered
    # Engineered prose blocks
    assert "Working rules" in rendered
    assert "Tool-use discipline" in rendered
    assert "Error recovery" in rendered
    # Plan / yolo bumpers always render *something* — either the active or
    # the inactive variant — so just check the section header exists.
    assert "# Plan mode" in rendered
    assert "# Yolo mode" in rendered
    # Wrap-up
    assert "Wrapping up" in rendered


def test_base_prompt_renders_plan_mode_section() -> None:
    """plan_mode=True flips the plan-mode bumper to the active variant."""
    pb = PromptBuilder()
    rendered = pb.build(plan_mode=True)
    assert "PLAN MODE" in rendered
    # Active variant explicitly mentions ExitPlanMode and the gate
    assert "ExitPlanMode" in rendered or "consent gate" in rendered.lower()
    # Inactive variant phrasing must NOT leak in when plan_mode is True
    assert "not currently in plan mode" not in rendered

    # And the inverse: with plan_mode=False the inactive variant wins
    inactive = pb.build(plan_mode=False)
    assert "not currently in plan mode" in inactive


def test_base_prompt_renders_memory_when_set() -> None:
    """The legacy <memory> slot still renders user-supplied content."""
    pb = PromptBuilder()
    rendered = pb.build(declarative_memory="user prefers concise responses")
    assert "<memory>" in rendered
    assert "concise responses" in rendered
    # And the user-profile + workspace + soul slots are still gated:
    assert "<user-profile>" not in rendered
    assert "<workspace-context>" not in rendered
    assert "Profile identity" not in rendered


def test_base_prompt_word_count_grew() -> None:
    """The rewrite must be substantively bigger than the 47-line stub."""
    base = (
        Path(__file__).parent.parent
        / "opencomputer"
        / "agent"
        / "prompts"
        / "base.j2"
    )
    line_count = len(base.read_text().splitlines())
    assert line_count >= 200, (
        f"base.j2 should be >=200 lines after the V3.A-T3 rewrite; got {line_count}"
    )
