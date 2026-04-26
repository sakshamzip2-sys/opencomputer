"""Tests for opencomputer.evolution.collision (Phase 5.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.evolution.collision import _tokenize, find_collision


def _write_skill(d: Path, slug: str, description: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: {description}\n---\n# {slug}\n"
    )


def test_tokenize_lowercase_and_min_length():
    assert _tokenize("Use when reviewing the SQL query") == {
        "use", "when", "reviewing", "the", "sql", "query",
    }
    # 1-2 char tokens excluded
    assert "a" not in _tokenize("a quick brown fox")
    assert "qu" not in _tokenize("qu pq abc")


def test_no_collision_when_descriptions_disjoint(tmp_path):
    bundled = tmp_path / "bundled"
    profile = tmp_path / "profile"
    _write_skill(bundled / "git-helper",
                 "git-helper",
                 "Use when committing changes via git workflow")
    draft = tmp_path / "draft.md"
    draft.write_text(
        "---\nname: pizza-orderer\ndescription: Use when ordering pizza online\n---\n"
    )
    assert find_collision(draft, bundled_skills_dir=bundled, profile_skills_dir=profile) is None


def test_collision_when_descriptions_match(tmp_path):
    bundled = tmp_path / "bundled"
    profile = tmp_path / "profile"
    _write_skill(bundled / "code-review",
                 "code-review",
                 "Use when reviewing pull requests for correctness")
    draft = tmp_path / "draft.md"
    # heavy overlap with existing
    draft.write_text(
        "---\nname: pr-review\ndescription: Use when reviewing pull requests for issues\n---\n"
    )
    collision = find_collision(
        draft, bundled_skills_dir=bundled, profile_skills_dir=profile
    )
    assert collision == "code-review"


def test_threshold_respected(tmp_path):
    bundled = tmp_path / "bundled"
    profile = tmp_path / "profile"
    _write_skill(bundled / "x", "x", "alpha beta gamma delta epsilon")
    # 2/5 = 40% overlap — below threshold 0.5
    draft = tmp_path / "draft.md"
    draft.write_text(
        "---\nname: y\ndescription: alpha beta omega kappa zeta eta theta\n---\n"
    )
    assert find_collision(
        draft, bundled_skills_dir=bundled, profile_skills_dir=profile,
        threshold=0.5,
    ) is None
    # Lower threshold → flagged
    assert find_collision(
        draft, bundled_skills_dir=bundled, profile_skills_dir=profile,
        threshold=0.4,
    ) == "x"


def test_corrupt_skill_md_does_not_crash(tmp_path):
    bundled = tmp_path / "bundled" / "broken"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("not valid frontmatter at all")
    draft = tmp_path / "draft.md"
    draft.write_text(
        "---\nname: y\ndescription: Use when doing thing\n---\n"
    )
    assert find_collision(
        draft, bundled_skills_dir=bundled.parent, profile_skills_dir=tmp_path / "ghost"
    ) is None


def test_missing_dirs_returns_none(tmp_path):
    draft = tmp_path / "draft.md"
    draft.write_text("---\nname: x\ndescription: Use when X\n---\n")
    assert find_collision(
        draft,
        bundled_skills_dir=tmp_path / "ghost1",
        profile_skills_dir=tmp_path / "ghost2",
    ) is None


def test_empty_draft_description_returns_none(tmp_path):
    bundled = tmp_path / "bundled"
    _write_skill(bundled / "x", "x", "Use when reviewing code")
    draft = tmp_path / "draft.md"
    draft.write_text("---\nname: y\ndescription:\n---\n")
    assert find_collision(
        draft, bundled_skills_dir=bundled, profile_skills_dir=tmp_path / "ghost"
    ) is None
