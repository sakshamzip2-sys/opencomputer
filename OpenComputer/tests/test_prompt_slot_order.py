"""D8: base.j2 slot order matches Hermes v2 spec.

Hermes v2 slot order:
  1. SOUL.md (agent identity)
  2. Tool-aware behavior guidance (Working rules + Tool-use discipline)
  3. Memory / user context (MEMORY + USER_PROFILE + user_facts + user_tone)
  4. Skills guidance
  5. Context files (workspace_context)
  6. Timestamp + platform formatting (System info)
  7. /personality + active persona overlay

Pin every slot ordering with a structural test so future template
edits can't silently regress to the old (mixed) order.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.memory import SkillMeta
from opencomputer.agent.prompt_builder import PromptBuilder


def _build_full_prompt() -> str:
    """Build a prompt with every slot populated so we can pin all 7 orderings."""
    return PromptBuilder().build(
        soul="# SOUL\nYour profile voicing.",
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
        user_facts="identity: senior engineer\ngoal: ship production code",
        workspace_context="## CLAUDE.md\n\nProject convention: pnpm only",
        personality="technical",  # resolves to a real body via builtins
        persona_overlay="Active persona: focus.",
    )


def test_soul_appears_before_working_rules():
    """Slot 1 (SOUL) before Slot 2 (Working rules)."""
    out = _build_full_prompt()
    assert "Profile identity" in out
    assert "Working rules" in out
    assert out.index("Profile identity") < out.index("Working rules")


def test_working_rules_appear_before_memory_integration():
    """Slot 2 (Working rules) before Slot 3 (Memory)."""
    out = _build_full_prompt()
    assert out.index("Working rules") < out.index("Memory integration")


def test_memory_integration_appears_before_skills():
    """Slot 3 (Memory) before Slot 4 (Skills available)."""
    out = _build_full_prompt()
    assert out.index("Memory integration") < out.index("Skills available")


def test_skills_appear_before_workspace_context():
    """Slot 4 (Skills) before Slot 5 (workspace-context)."""
    out = _build_full_prompt()
    assert out.index("Skills available") < out.index("<workspace-context>")


def test_workspace_context_appears_before_system_info():
    """Slot 5 (workspace_context) before Slot 6 (System info / timestamp)."""
    out = _build_full_prompt()
    assert out.index("<workspace-context>") < out.index("System info")


def test_system_info_appears_before_personality():
    """Slot 6 (System info) before Slot 7 (Personality directive)."""
    out = _build_full_prompt()
    assert out.index("System info") < out.index("Personality directive")


def test_persona_overlay_appears_before_personality_directive():
    """Slot 7 has BOTH active-persona overlay AND personality directive.
    Both follow system info, in that order."""
    out = _build_full_prompt()
    assert out.index("System info") < out.index("Active persona")
    assert out.index("Active persona") < out.index("Personality directive")


def test_full_slot_order_is_canonical():
    """All seven slots in canonical Hermes order.

    Pinning the entire chain in one test catches any drift more loudly
    than the per-pair tests above (which still help diagnose which
    boundary moved)."""
    out = _build_full_prompt()
    markers = [
        "Profile identity",       # slot 1
        "Working rules",          # slot 2
        "Memory integration",     # slot 3
        "Skills available",       # slot 4
        "<workspace-context>",    # slot 5
        "System info",            # slot 6
        "Active persona",         # slot 7a
        "Personality directive",  # slot 7b
    ]
    indices = [out.index(m) for m in markers]
    assert indices == sorted(indices), (
        f"slot order not strictly increasing — got "
        f"{dict(zip(markers, indices, strict=True))}"
    )


def test_empty_optional_slots_are_omitted_cleanly():
    """When SOUL / skills / workspace / personality are empty, those
    sections should not appear at all (no empty headers)."""
    out = PromptBuilder().build(
        soul="",
        skills=None,
        declarative_memory="",
        user_profile="",
        user_facts="",
        workspace_context="",
        personality="",
    )
    assert "Profile identity" not in out
    assert "Skills available" not in out
    assert "<workspace-context>" not in out
    assert "Personality directive" not in out
    # Working rules + System info always render.
    assert "Working rules" in out
    assert "System info" in out
