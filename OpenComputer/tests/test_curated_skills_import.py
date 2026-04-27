"""tests/test_curated_skills_import.py — verify the 10 newly-imported skills are discoverable."""
from __future__ import annotations

from pathlib import Path

import pytest

# Skills imported from everything-claude-code (MIT) on 2026-04-27.
_NEW_SKILLS: tuple[str, ...] = (
    "prp-prd",
    "prp-plan",
    "prp-implement",
    "prp-pr",
    "prp-commit",
    "model-route",
    "silent-failure-hunter",
    "gan-evaluator",
    "gan-generator",
    "gan-planner",
)


def _skills_root() -> Path:
    return Path(__file__).resolve().parent.parent / "opencomputer" / "skills"


@pytest.mark.parametrize("skill_name", _NEW_SKILLS)
def test_skill_directory_and_file_exist(skill_name: str):
    skill_md = _skills_root() / skill_name / "SKILL.md"
    assert skill_md.exists(), f"missing SKILL.md for {skill_name}"


@pytest.mark.parametrize("skill_name", _NEW_SKILLS)
def test_skill_frontmatter_has_name_and_description(skill_name: str):
    skill_md = _skills_root() / skill_name / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{skill_name} missing frontmatter opener"
    # Frontmatter ends with --- on its own line; pull it
    parts = text.split("---", 2)
    assert len(parts) >= 3, f"{skill_name} frontmatter not closed"
    fm = parts[1]
    assert "name:" in fm, f"{skill_name} frontmatter missing 'name:'"
    assert "description:" in fm, f"{skill_name} frontmatter missing 'description:'"


@pytest.mark.parametrize("skill_name", _NEW_SKILLS)
def test_skill_name_matches_directory(skill_name: str):
    skill_md = _skills_root() / skill_name / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    fm = parts[1]
    name_line = next((line for line in fm.splitlines() if line.strip().startswith("name:")), None)
    assert name_line is not None
    declared = name_line.split(":", 1)[1].strip().strip('"').strip("'")
    assert declared == skill_name, f"{skill_name}: frontmatter declares name={declared!r}"


@pytest.mark.parametrize("skill_name", _NEW_SKILLS)
def test_skill_has_source_attribution(skill_name: str):
    skill_md = _skills_root() / skill_name / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    assert "everything-claude-code" in text and "MIT" in text, (
        f"{skill_name}: missing source attribution comment"
    )


@pytest.mark.parametrize("skill_name", _NEW_SKILLS)
def test_skill_body_is_nontrivial(skill_name: str):
    skill_md = _skills_root() / skill_name / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    body = text.split("---", 2)[2]
    # Body must have at least 200 chars of actual content
    body_no_whitespace = body.strip()
    assert len(body_no_whitespace) >= 200, (
        f"{skill_name}: body suspiciously short ({len(body_no_whitespace)} chars)"
    )


def test_all_ten_skills_imported():
    """Sanity: count test."""
    found = sum(1 for s in _NEW_SKILLS if (_skills_root() / s / "SKILL.md").exists())
    assert found == 10, f"expected 10 imported skills, found {found}"
