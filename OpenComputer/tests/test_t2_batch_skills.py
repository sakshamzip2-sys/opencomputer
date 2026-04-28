"""tests/test_t2_batch_skills.py — verify T2 batch SKILL.md files are well-formed."""
from __future__ import annotations

from pathlib import Path

import pytest

_NEW_SKILLS = (
    "meeting-notes",
    "inbox-triage",
    "bill-deadline-tracker",
    "coding-via-chat",
)


def _skills_root() -> Path:
    return Path(__file__).resolve().parent.parent / "opencomputer" / "skills"


@pytest.mark.parametrize("skill_name", _NEW_SKILLS)
def test_skill_directory_and_file_exist(skill_name):
    p = _skills_root() / skill_name / "SKILL.md"
    assert p.exists(), f"missing SKILL.md for {skill_name}"


@pytest.mark.parametrize("skill_name", _NEW_SKILLS)
def test_skill_frontmatter_well_formed(skill_name):
    text = (_skills_root() / skill_name / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    parts = text.split("---", 2)
    assert len(parts) >= 3
    fm = parts[1]
    assert "name:" in fm
    assert "description:" in fm


@pytest.mark.parametrize("skill_name", _NEW_SKILLS)
def test_skill_name_matches_directory(skill_name):
    text = (_skills_root() / skill_name / "SKILL.md").read_text(encoding="utf-8")
    fm = text.split("---", 2)[1]
    name_line = next(line for line in fm.splitlines() if line.strip().startswith("name:"))
    declared = name_line.split(":", 1)[1].strip().strip('"').strip("'")
    assert declared == skill_name


@pytest.mark.parametrize("skill_name", _NEW_SKILLS)
def test_skill_body_has_substance(skill_name):
    text = (_skills_root() / skill_name / "SKILL.md").read_text(encoding="utf-8")
    body = text.split("---", 2)[2].strip()
    assert len(body) >= 500, f"{skill_name} body suspiciously short ({len(body)} chars)"


def test_all_4_skills_present():
    found = sum(1 for s in _NEW_SKILLS if (_skills_root() / s / "SKILL.md").exists())
    assert found == 4
