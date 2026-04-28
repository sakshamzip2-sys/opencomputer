"""Skill loader must discover skills installed under ``.hub/<source>/<name>/``.

Without this, the Skills Hub is decorative: ``oc skills install`` would write
SKILL.md files the agent never sees because ``MemoryManager.list_skills``
only walks one level under each root.

This test pins the contract that hub-installed skills appear in
``list_skills()`` alongside user-authored ones, with hub source dirs treated
as additional roots (lowest priority — user dir still shadows on collision).
"""

from __future__ import annotations

from pathlib import Path

from opencomputer.agent.memory import MemoryManager


def _seed_skill(at: Path, name: str = "hub-test-skill") -> Path:
    """Write a minimum-valid SKILL.md at ``at/<name>/SKILL.md`` with matching frontmatter name."""
    skill_dir = at / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(
        f"---\n"
        f"name: {name}\n"
        f"description: A test skill installed under the hub directory layout for loader-pickup verification\n"
        f"version: 1.0.0\n"
        f"---\n"
        f"\n"
        f"# {name}\n"
    )
    return md


def _make_manager(tmp_path: Path) -> MemoryManager:
    return MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=tmp_path / "skills",
        bundled_skills_paths=[],
    )


def test_user_skill_in_root_is_discovered(tmp_path):
    """Sanity check: user-authored skill at <skills>/foo/SKILL.md is listed."""
    mgr = _make_manager(tmp_path)
    _seed_skill(tmp_path / "skills", name="user-only")
    skills = mgr.list_skills()
    names = {s.name for s in skills}
    assert "user-only" in names


def test_skill_in_hub_source_dir_is_discovered(tmp_path):
    """Hub-installed skills at <skills>/.hub/<source>/<name>/SKILL.md must be listed.

    Layout:
        <skills>/
          .hub/
            well-known/
              hub-test-skill/
                SKILL.md
    """
    mgr = _make_manager(tmp_path)
    hub_source = tmp_path / "skills" / ".hub" / "well-known"
    _seed_skill(hub_source, name="hub-test-skill")
    skills = mgr.list_skills()
    names = {s.name for s in skills}
    assert "hub-test-skill" in names


def test_hub_skills_from_multiple_sources_all_discovered(tmp_path):
    """Each <source> dir under .hub/ contributes its own skills."""
    mgr = _make_manager(tmp_path)
    _seed_skill(tmp_path / "skills" / ".hub" / "well-known", name="from-well-known")
    _seed_skill(tmp_path / "skills" / ".hub" / "alice-cool-skills", name="from-alice")
    skills = mgr.list_skills()
    names = {s.name for s in skills}
    assert "from-well-known" in names
    assert "from-alice" in names


def test_user_skill_shadows_hub_on_id_collision(tmp_path):
    """When user dir and hub source both define a skill with the same dir-name,
    the user dir wins (existing shadowing semantics preserved)."""
    mgr = _make_manager(tmp_path)
    # User version: description starts with "USER"
    user_dir = tmp_path / "skills" / "shared-name"
    user_dir.mkdir(parents=True)
    (user_dir / "SKILL.md").write_text(
        "---\nname: shared-name\ndescription: USER VERSION takes priority over hub copy\n---\n"
    )
    # Hub version: description starts with "HUB"
    hub_dir = tmp_path / "skills" / ".hub" / "well-known" / "shared-name"
    hub_dir.mkdir(parents=True)
    (hub_dir / "SKILL.md").write_text(
        "---\nname: shared-name\ndescription: HUB VERSION should be shadowed by user version\n---\n"
    )
    skills = mgr.list_skills()
    matches = [s for s in skills if s.name == "shared-name"]
    assert len(matches) == 1, f"expected exactly one shared-name, got {len(matches)}"
    assert matches[0].description.startswith("USER VERSION")


def test_hub_root_missing_does_not_break_listing(tmp_path):
    """No .hub/ directory at all — listing still works, returns user skills only."""
    mgr = _make_manager(tmp_path)
    _seed_skill(tmp_path / "skills", name="user-only")
    # Don't create .hub/ at all
    skills = mgr.list_skills()
    names = {s.name for s in skills}
    assert names == {"user-only"}


def test_hub_root_empty_does_not_break_listing(tmp_path):
    """Empty .hub/ directory — listing still works."""
    mgr = _make_manager(tmp_path)
    _seed_skill(tmp_path / "skills", name="user-only")
    (tmp_path / "skills" / ".hub").mkdir(parents=True)
    skills = mgr.list_skills()
    names = {s.name for s in skills}
    assert names == {"user-only"}


def test_hub_source_dir_with_no_skills_inside_is_safely_skipped(tmp_path):
    """A source dir with no skill subdirs (e.g. mid-install state) doesn't crash."""
    mgr = _make_manager(tmp_path)
    _seed_skill(tmp_path / "skills", name="user-only")
    (tmp_path / "skills" / ".hub" / "empty-source").mkdir(parents=True)
    skills = mgr.list_skills()
    names = {s.name for s in skills}
    assert "user-only" in names
