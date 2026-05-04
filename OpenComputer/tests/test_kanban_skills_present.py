"""Smoke test for the kanban-worker + kanban-orchestrator skills (Wave 6.B-γ).

Closes a dangling reference: the system-prompt KANBAN_GUIDANCE block
points workers + orchestrators to these skills for deeper detail. Up
through Wave 6.B (PR #429) only ``kanban-video-orchestrator`` shipped;
this verifies both foundational skills are now present + parseable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.skills_hub.agentskills_validator import validate_frontmatter

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "opencomputer" / "skills"


@pytest.mark.parametrize("skill_id", ["kanban-worker", "kanban-orchestrator"])
def test_skill_directory_exists(skill_id: str):
    d = SKILLS_ROOT / skill_id
    assert d.is_dir(), f"missing skill directory: {d}"
    assert (d / "SKILL.md").is_file(), f"missing SKILL.md in {d}"


@pytest.mark.parametrize("skill_id", ["kanban-worker", "kanban-orchestrator"])
def test_skill_frontmatter_valid(skill_id: str):
    text = (SKILLS_ROOT / skill_id / "SKILL.md").read_text()
    parsed = validate_frontmatter(text)
    assert parsed["name"] == skill_id


@pytest.mark.parametrize("skill_id", ["kanban-worker", "kanban-orchestrator"])
def test_no_hermes_env_var_leaks(skill_id: str):
    """Verbatim port should have renamed HERMES_* → OC_*. Catch missed renames."""
    text = (SKILLS_ROOT / skill_id / "SKILL.md").read_text()
    assert "HERMES_KANBAN_TASK" not in text
    assert "HERMES_KANBAN_WORKSPACE" not in text
    assert "HERMES_KANBAN_DB" not in text
    # Hermes-specific shell command names should be renamed
    assert "hermes kanban" not in text.lower()


def test_kanban_video_orchestrator_still_present():
    """Regression check — PR #429's skill must still ship alongside."""
    assert (SKILLS_ROOT / "kanban-video-orchestrator" / "SKILL.md").is_file()
