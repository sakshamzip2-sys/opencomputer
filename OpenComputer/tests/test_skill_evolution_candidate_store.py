"""tests/test_skill_evolution_candidate_store.py"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from extensions.skill_evolution.candidate_store import (
    accept_candidate,
    add_candidate,
    get_candidate,
    list_candidates,
    prune_old_candidates,
    reject_candidate,
)
from extensions.skill_evolution.skill_extractor import ProposedSkill


def _make_proposal(
    name: str = "auto-abc12345-test",
    description: str = "Use when testing",
    body: str | None = None,
) -> ProposedSkill:
    return ProposedSkill(
        name=name,
        description=description,
        body=body
        or f"---\nname: {name}\ndescription: {description}\n---\n\n# Test\n\nbody content",
        provenance={
            "session_id": "abc12345",
            "generated_at": time.time(),
            "confidence_score": 85,
            "source_summary": "test pattern",
        },
    )


def test_add_creates_files(tmp_path):
    p = _make_proposal()
    written = add_candidate(tmp_path, p)
    assert (tmp_path / "skills" / "_proposed" / p.name / "SKILL.md").exists()
    assert (tmp_path / "skills" / "_proposed" / p.name / "provenance.json").exists()
    assert written == tmp_path / "skills" / "_proposed" / p.name


def test_add_collision_appends_suffix(tmp_path):
    p = _make_proposal(name="auto-x-y")
    written1 = add_candidate(tmp_path, p)
    written2 = add_candidate(tmp_path, p)  # same name, should suffix
    assert written1.name == "auto-x-y"
    assert written2.name == "auto-x-y-2"
    # Both still exist
    assert (tmp_path / "skills" / "_proposed" / "auto-x-y").exists()
    assert (tmp_path / "skills" / "_proposed" / "auto-x-y-2").exists()


def test_list_returns_sorted_newest_first(tmp_path):
    older = _make_proposal(name="auto-old")
    older.provenance["generated_at"] = time.time() - 1000
    add_candidate(tmp_path, older)

    newer = _make_proposal(name="auto-new")
    add_candidate(tmp_path, newer)

    items = list_candidates(tmp_path)
    assert len(items) == 2
    assert items[0].name == "auto-new"  # newest first
    assert items[1].name == "auto-old"


def test_list_empty_when_no_proposals(tmp_path):
    assert list_candidates(tmp_path) == []


def test_get_candidate_returns_full_skill(tmp_path):
    p = _make_proposal()
    add_candidate(tmp_path, p)
    loaded = get_candidate(tmp_path, p.name)
    assert loaded is not None
    assert loaded.name == p.name
    assert loaded.description == p.description
    assert "name: " + p.name in loaded.body
    assert loaded.provenance["confidence_score"] == 85


def test_get_candidate_missing_returns_none(tmp_path):
    assert get_candidate(tmp_path, "auto-missing") is None


def test_accept_moves_to_active(tmp_path):
    p = _make_proposal()
    add_candidate(tmp_path, p)
    new_path = accept_candidate(tmp_path, p.name)
    assert new_path == tmp_path / "skills" / p.name
    # Original gone
    assert not (tmp_path / "skills" / "_proposed" / p.name).exists()
    # New active exists with SKILL.md
    assert (new_path / "SKILL.md").exists()


def test_accept_rejects_collision_with_active(tmp_path):
    """Active <name>/ already exists → accept refuses."""
    active = tmp_path / "skills" / "auto-conflict"
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text("existing")

    p = _make_proposal(name="auto-conflict")
    add_candidate(tmp_path, p)
    with pytest.raises(FileExistsError):
        accept_candidate(tmp_path, p.name)


def test_reject_deletes_proposal(tmp_path):
    p = _make_proposal()
    add_candidate(tmp_path, p)
    assert reject_candidate(tmp_path, p.name) is True
    assert not (tmp_path / "skills" / "_proposed" / p.name).exists()


def test_reject_missing_returns_false(tmp_path):
    assert reject_candidate(tmp_path, "auto-nonexistent") is False


def test_prune_old_candidates(tmp_path):
    old = _make_proposal(name="auto-old")
    old.provenance["generated_at"] = time.time() - 100 * 86400  # 100 days ago
    add_candidate(tmp_path, old)

    fresh = _make_proposal(name="auto-fresh")
    add_candidate(tmp_path, fresh)

    pruned = prune_old_candidates(tmp_path, max_age_days=90)
    assert pruned == 1
    assert list_candidates(tmp_path)[0].name == "auto-fresh"


def test_prune_keeps_all_when_threshold_high(tmp_path):
    p = _make_proposal()
    add_candidate(tmp_path, p)
    pruned = prune_old_candidates(tmp_path, max_age_days=10000)
    assert pruned == 0


def test_atomic_add_no_partial_state(tmp_path, monkeypatch):
    """If write fails mid-way (e.g. SKILL.md written but provenance.json fails),
    no half-written candidate should be visible to list_candidates."""
    p = _make_proposal()

    # Simulate failure during JSON write
    original_write = Path.write_text
    written_paths: list[str] = []

    def failing_write(self, *args, **kwargs):
        written_paths.append(str(self))
        if self.name == "provenance.json":
            raise OSError("simulated disk error")
        return original_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write)
    with pytest.raises(OSError):
        add_candidate(tmp_path, p)

    # After failure, list should be empty (no partial state)
    monkeypatch.undo()
    assert list_candidates(tmp_path) == []
