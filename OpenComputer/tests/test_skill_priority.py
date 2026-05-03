"""v0.5 Item: skill priority weighting infrastructure.

Adds optional ``priority:`` frontmatter field on SKILL.md. Higher
priority surfaces earlier. Skills without the field keep alphabetical
ordering (zero behavior change for v0 skills).
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.memory import MemoryManager


def _write_skill(skills_dir: Path, skill_id: str, priority: float | None = None):
    d = skills_dir / skill_id
    d.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---", f"name: {skill_id}", "description: t"]
    if priority is not None:
        fm_lines.append(f"priority: {priority}")
    fm_lines.append("---")
    fm_lines.append("body")
    (d / "SKILL.md").write_text("\n".join(fm_lines))


def test_unweighted_skills_remain_alphabetical(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(skills, "zebra")
    _write_skill(skills, "alpha")
    _write_skill(skills, "mango")

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills,
        bundled_skills_paths=[],
    )
    ids = [s.id for s in mm.list_skills()]
    assert ids == ["alpha", "mango", "zebra"]


def test_high_priority_skill_surfaces_first(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(skills, "alpha", priority=1.0)
    _write_skill(skills, "zebra", priority=10.0)
    _write_skill(skills, "mango")  # unweighted

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills,
        bundled_skills_paths=[],
    )
    ids = [s.id for s in mm.list_skills()]
    # priority=10 first, priority=1 next, unweighted last (alphabetical
    # tail just one item here)
    assert ids[0] == "zebra"
    assert ids[1] == "alpha"
    assert ids[2] == "mango"


def test_priority_field_round_trips(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(skills, "weighted", priority=42.5)

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills,
        bundled_skills_paths=[],
    )
    skill = mm.list_skills()[0]
    assert skill.priority == 42.5


def test_invalid_priority_treated_as_none(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    skill_dir = skills / "broken"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: broken\ndescription: t\npriority: not-a-number\n---\nbody\n"
    )

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills,
        bundled_skills_paths=[],
    )
    skill = mm.list_skills()[0]
    assert skill.priority is None
