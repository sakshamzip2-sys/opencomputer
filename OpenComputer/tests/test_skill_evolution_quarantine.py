"""Tests for the quarantine state in extensions/skill-evolution/candidate_store.

Phase 5 — Skill Workshop state machine completion (B1 leftover from
2026-05-06 OpenClaw deep-comparison).
"""

from __future__ import annotations

import json
from pathlib import Path

# Skill-evolution lives under extensions/ — pyproject pytest config exposes
# it as a top-level import alias 'extensions.skill_evolution' (see existing
# tests/test_skill_evolution_pattern_detector.py).
from extensions.skill_evolution import candidate_store as cs


def _load_candidate_store():
    """Helper kept for symmetry with other test files; returns the module."""
    return cs


def _seed_proposal(profile_home: Path, name: str = "test-skill") -> Path:
    """Drop a minimal candidate dir into _proposed/<name>/."""
    proposal = profile_home / "skills" / "_proposed" / name
    proposal.mkdir(parents=True, exist_ok=True)
    (proposal / "SKILL.md").write_text(
        "---\nname: " + name + "\ndescription: test\n---\nbody\n"
    )
    return proposal


def test_quarantine_moves_candidate(tmp_path: Path):
    cs = _load_candidate_store()
    _seed_proposal(tmp_path, "to-quarantine")

    ok = cs.quarantine_candidate(
        tmp_path, "to-quarantine", reason="duplicate of existing skill"
    )
    assert ok is True

    # Original location empty.
    assert not (tmp_path / "skills" / "_proposed" / "to-quarantine").exists()

    # Quarantine dir contains it + metadata.
    qdir = tmp_path / "skills" / "_proposed" / ".quarantined" / "to-quarantine"
    assert qdir.is_dir()
    meta = json.loads((qdir / "quarantine_meta.json").read_text())
    assert meta["reason"] == "duplicate of existing skill"
    assert meta["name"] == "to-quarantine"
    assert isinstance(meta["quarantined_at"], (int, float))


def test_quarantine_unknown_name_returns_false(tmp_path: Path):
    cs = _load_candidate_store()
    assert cs.quarantine_candidate(tmp_path, "ghost", reason="x") is False


def test_quarantine_collision_resolved(tmp_path: Path):
    """Quarantining two candidates with the same name → numeric suffix."""
    cs = _load_candidate_store()
    _seed_proposal(tmp_path, "dup")
    cs.quarantine_candidate(tmp_path, "dup", reason="r1")
    _seed_proposal(tmp_path, "dup")
    cs.quarantine_candidate(tmp_path, "dup", reason="r2")

    qdir = tmp_path / "skills" / "_proposed" / ".quarantined"
    names = sorted(d.name for d in qdir.iterdir() if d.is_dir())
    assert names == ["dup", "dup-2"]


def test_list_quarantined_empty(tmp_path: Path):
    cs = _load_candidate_store()
    assert cs.list_quarantined(tmp_path) == []


def test_list_quarantined_returns_meta(tmp_path: Path):
    cs = _load_candidate_store()
    _seed_proposal(tmp_path, "alpha")
    _seed_proposal(tmp_path, "beta")
    cs.quarantine_candidate(tmp_path, "alpha", reason="a-reason")
    cs.quarantine_candidate(tmp_path, "beta", reason="b-reason")

    metas = cs.list_quarantined(tmp_path)
    assert len(metas) == 2
    by_name = {m.name: m for m in metas}
    assert by_name["alpha"].reason == "a-reason"
    assert by_name["beta"].reason == "b-reason"
    assert all(m.age_days >= 0 for m in metas)


def test_unquarantine_restores_to_proposed(tmp_path: Path):
    cs = _load_candidate_store()
    _seed_proposal(tmp_path, "restore-me")
    cs.quarantine_candidate(tmp_path, "restore-me", reason="oops")

    ok = cs.unquarantine_candidate(tmp_path, "restore-me")
    assert ok is True

    proposed = tmp_path / "skills" / "_proposed" / "restore-me"
    assert proposed.is_dir()
    # Metadata file should not survive the restore.
    assert not (proposed / "quarantine_meta.json").exists()
    # Original SKILL.md preserved.
    assert (proposed / "SKILL.md").exists()


def test_unquarantine_unknown_returns_false(tmp_path: Path):
    cs = _load_candidate_store()
    assert cs.unquarantine_candidate(tmp_path, "ghost") is False


def test_purge_quarantined_respects_age(tmp_path: Path):
    cs = _load_candidate_store()
    _seed_proposal(tmp_path, "old")
    _seed_proposal(tmp_path, "new")
    cs.quarantine_candidate(tmp_path, "old", reason="r")
    cs.quarantine_candidate(tmp_path, "new", reason="r")

    # Backdate "old" by 60 days.
    qdir = tmp_path / "skills" / "_proposed" / ".quarantined"
    old_meta = qdir / "old" / "quarantine_meta.json"
    meta = json.loads(old_meta.read_text())
    import time

    meta["quarantined_at"] = time.time() - 60 * 86400
    old_meta.write_text(json.dumps(meta))

    purged = cs.purge_quarantined(tmp_path, max_age_days=30)
    assert purged == 1
    survivors = [m.name for m in cs.list_quarantined(tmp_path)]
    assert survivors == ["new"]


def test_purge_quarantined_empty_dir_zero(tmp_path: Path):
    cs = _load_candidate_store()
    assert cs.purge_quarantined(tmp_path, max_age_days=30) == 0
