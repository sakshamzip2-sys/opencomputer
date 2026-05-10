"""Hermes v2 D8 — slot order holds under all conditional branches in base.j2.

PR #515 added `tests/test_prompt_slot_order.py` which exercises the
default branch (active_persona_id != "companion"). This file exercises
the OTHER branches: companion mode, no-persona-overlay, no-soul, etc.,
so the slot order can't silently regress under any conditional.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.memory import SkillMeta
from opencomputer.agent.prompt_builder import PromptBuilder

_FULL_KWARGS = dict(
    soul="# SOUL\nProfile voicing.",
    skills=[
        SkillMeta(
            id="example-skill",
            name="example-skill",
            path=Path("opencomputer/skills/example/SKILL.md"),
            description="Example skill description.",
        )
    ],
    declarative_memory="user prefers concise responses",
    user_profile="role: senior engineer",
    user_facts="identity: senior engineer",
    workspace_context="## CLAUDE.md\n\nProject convention: pnpm only",
    personality="technical",
    persona_overlay="Active persona: focus.",
)

_SLOT_MARKERS = [
    "Profile identity",
    "Working rules",
    "Memory integration",
    "Skills available",
    "<workspace-context>",
    "System info",
    "## Active persona",
    "Personality directive",
]


def _slot_indices(out: str) -> list[int]:
    return [out.index(m) for m in _SLOT_MARKERS]


def test_slot_order_under_companion_mode():
    """Companion mode preserves Hermes slot order."""
    out = PromptBuilder().build(**_FULL_KWARGS, active_persona_id="companion")
    indices = _slot_indices(out)
    assert indices == sorted(indices)
    # Companion-specific copy renders.
    assert "social one" in out


def test_slot_order_under_default_persona():
    """Non-companion (default) persona preserves slot order."""
    out = PromptBuilder().build(**_FULL_KWARGS, active_persona_id="default")
    indices = _slot_indices(out)
    assert indices == sorted(indices)
    # Non-companion preamble renders.
    assert "not a chat toy" in out


def test_slot_order_no_personality():
    """No personality → Personality directive omitted but Active persona still shown."""
    kwargs = dict(_FULL_KWARGS)
    kwargs.pop("personality")
    kwargs.pop("persona_overlay")  # also no persona
    out = PromptBuilder().build(**kwargs)
    # Slot 7 markers should both be absent.
    assert "Personality directive" not in out
    assert "Active persona" not in out
    # Other slots still present + ordered.
    markers = [
        "Profile identity",
        "Working rules",
        "Memory integration",
        "Skills available",
        "<workspace-context>",
        "System info",
    ]
    indices = [out.index(m) for m in markers]
    assert indices == sorted(indices)


def test_slot_order_no_workspace_context():
    """No workspace context → slot 5 omitted, others still ordered."""
    kwargs = dict(_FULL_KWARGS)
    kwargs["workspace_context"] = ""
    out = PromptBuilder().build(**kwargs)
    assert "<workspace-context>" not in out
    markers = [
        "Profile identity",
        "Working rules",
        "Memory integration",
        "Skills available",
        # workspace omitted
        "System info",
        "## Active persona",
        "Personality directive",
    ]
    indices = [out.index(m) for m in markers]
    assert indices == sorted(indices)


def test_slot_order_no_soul():
    """No SOUL.md → slot 1 (Profile identity) omitted, others ordered."""
    kwargs = dict(_FULL_KWARGS)
    kwargs["soul"] = ""
    out = PromptBuilder().build(**kwargs)
    assert "Profile identity" not in out
    markers = [
        "Working rules",
        "Memory integration",
        "Skills available",
        "<workspace-context>",
        "System info",
        "## Active persona",
        "Personality directive",
    ]
    indices = [out.index(m) for m in markers]
    assert indices == sorted(indices)


def test_slot_order_companion_no_skills():
    """Companion mode + no skills → still ordered without slot 4."""
    kwargs = dict(_FULL_KWARGS)
    kwargs["skills"] = None
    out = PromptBuilder().build(**kwargs, active_persona_id="companion")
    assert "Skills available" not in out
    markers = [
        "Profile identity",
        "Working rules",
        "Memory integration",
        "<workspace-context>",
        "System info",
        "## Active persona",
        "Personality directive",
    ]
    indices = [out.index(m) for m in markers]
    assert indices == sorted(indices)


def test_user_tone_renders_inside_memory_block():
    """user_tone takes precedence over persona_preferred_tone within slot 3."""
    kwargs = dict(_FULL_KWARGS)
    kwargs["persona_overlay"] = ""
    out = PromptBuilder().build(
        **kwargs,
        user_tone="prefer terse, technical responses",
        persona_preferred_tone="warm reflective",
    )
    # user-tone block renders; persona-tone does NOT (precedence rule).
    assert "<user-tone>" in out
    assert "<persona-tone>" not in out
    # And it's in slot 3 (between Memory integration and Skills/...).
    user_tone_idx = out.index("<user-tone>")
    memory_idx = out.index("Memory integration")
    skills_idx = out.index("Skills available")
    assert memory_idx < user_tone_idx < skills_idx


def test_persona_preferred_tone_renders_when_no_user_tone():
    """When user_tone is empty, persona_preferred_tone fills slot 3 tone block."""
    kwargs = dict(_FULL_KWARGS)
    kwargs["persona_overlay"] = ""
    out = PromptBuilder().build(
        **kwargs,
        user_tone="",
        persona_preferred_tone="warm reflective",
    )
    assert "<user-tone>" not in out
    assert "<persona-tone>" in out
    assert "warm reflective" in out
