"""Phase 12f: curated skill import.

15 universal workflow skills imported from claude-plugins-official into
opencomputer/skills/. They join the lone existing bundled skill
(debug-python-import-error) and are picked up by the same
MemoryManager.list_skills path that the agent already uses to inject
skill descriptions into the system prompt.

Each imported skill carries an HTML-comment attribution + required-tools
note so future maintainers know:
- where the skill came from (license attribution)
- which tools it assumes (so coding-harness-required skills don't
  surprise users running OpenComputer chat-only)

This test asserts:
- All 15 named skills load through MemoryManager (frontmatter parses, name+
  description present).
- Each carries the source attribution comment.
- Each carries the required-tools comment.
- The pre-existing bundled skill still loads (no regression).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The 15 skills the importer brought in.
EXPECTED_NEW_SKILLS: tuple[str, ...] = (
    # superpowers (10)
    "brainstorming",
    "writing-plans",
    "executing-plans",
    "systematic-debugging",
    "finishing-a-development-branch",
    "using-git-worktrees",
    "subagent-driven-development",
    "test-driven-development",
    "dispatching-parallel-agents",
    "verification-before-completion",
    # everything-claude-code (5)
    "coding-standards",
    "tdd-workflow",
    "verification-loop",
    "security-review",
    "continuous-learning",
)

#: Pre-existing skill that must still load (regression guard).
LEGACY_SKILL = "debug-python-import-error"


def _bundled_skills_root() -> Path:
    return Path(__file__).resolve().parent.parent / "opencomputer" / "skills"


def test_all_15_imported_skills_load_via_memory_manager(tmp_path: Path) -> None:
    """The agent picks up bundled skills through `MemoryManager.list_skills`,
    which lists user skills first then bundled. Pointing it at an empty user
    dir forces it to read only the bundled ones we shipped this PR."""
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "user-skills"
    user_skills.mkdir()
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
    )
    skills = mm.list_skills()
    by_id = {s.id: s for s in skills}

    for name in EXPECTED_NEW_SKILLS:
        assert name in by_id, f"missing imported skill: {name}"
        meta = by_id[name]
        assert meta.description, f"empty description on {name!r}"
        # The frontmatter has a `name` field that may differ from the dir id;
        # both should be non-empty strings.
        assert meta.name, f"empty name field on {name!r}"


def test_legacy_bundled_skill_still_loads(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "user-skills"
    user_skills.mkdir()
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
    )
    ids = {s.id for s in mm.list_skills()}
    assert LEGACY_SKILL in ids, "regression: existing bundled skill no longer loads"


@pytest.mark.parametrize("skill_name", EXPECTED_NEW_SKILLS)
def test_each_imported_skill_carries_source_and_required_tools(skill_name: str) -> None:
    """Every imported skill must have the attribution + required-tools comment
    block we added during import. Without these, the source is forgotten and
    coding-harness-required skills surprise users."""
    skill_md = _bundled_skills_root() / skill_name / "SKILL.md"
    assert skill_md.exists(), f"{skill_name}/SKILL.md missing"
    body = skill_md.read_text(encoding="utf-8")
    assert "<!-- Source:" in body, f"{skill_name}: missing source attribution"
    assert "<!-- Required tools:" in body, f"{skill_name}: missing required-tools note"


def test_total_bundled_skills_count_matches_expected() -> None:
    """16 total = 15 newly imported + 1 pre-existing (debug-python-import-error)."""
    bundled = [
        d for d in _bundled_skills_root().iterdir() if d.is_dir() and (d / "SKILL.md").exists()
    ]
    bundled_ids = {d.name for d in bundled}
    expected = set(EXPECTED_NEW_SKILLS) | {LEGACY_SKILL}
    # Allow the bundled set to include either exactly our list, or that list
    # plus any legacy skills future PRs might add — but never less.
    assert expected <= bundled_ids, f"missing bundled skills: {expected - bundled_ids}"
