"""Tests for SkillSynthesizer.synthesize() — full synthesis flow (B2.4).

Covers III.4 hierarchical layout, atomic write, slug collision, path-traversal
guard, and traceability metadata in SKILL.md.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.evolution.reflect import Insight  # noqa: F401
from opencomputer.evolution.synthesize import SkillSynthesizer

# ---------------------------------------------------------------------------
# Helper: build a valid create_skill Insight
# ---------------------------------------------------------------------------


def _create_skill_insight(**overrides):
    payload = {
        "slug": "read-then-edit",
        "name": "Read-then-Edit",
        "description": "Use after a file Read when you intend to edit",
        "body": "# Read-then-Edit\n\nReads then immediately edits a file.",
    }
    payload.update(overrides.pop("payload_overrides", {}))
    defaults = dict(
        observation="Read followed by Edit is common",
        evidence_refs=(1, 2, 3),
        action_type="create_skill",
        payload=payload,
        confidence=0.85,
    )
    defaults.update(overrides)
    return Insight(**defaults)


# ---------------------------------------------------------------------------
# 1. Creates dir and SKILL.md
# ---------------------------------------------------------------------------


def test_synthesize_creates_dir_and_skill_md(tmp_path: Path) -> None:
    """synthesize() creates <dest>/read-then-edit/SKILL.md with frontmatter and body."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    synth.synthesize(_create_skill_insight())

    skill_md = tmp_path / "read-then-edit" / "SKILL.md"
    assert skill_md.exists(), "SKILL.md was not created"
    content = skill_md.read_text(encoding="utf-8")
    assert "name: Read-then-Edit" in content
    assert "Reads then immediately edits a file." in content


# ---------------------------------------------------------------------------
# 2. Returns the final directory path
# ---------------------------------------------------------------------------


def test_synthesize_returns_final_path(tmp_path: Path) -> None:
    """synthesize() return value is the skill dir and it is a directory."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    result = synth.synthesize(_create_skill_insight())

    assert result == tmp_path / "read-then-edit"
    assert result.is_dir()


# ---------------------------------------------------------------------------
# 3. Quarantine marker is present
# ---------------------------------------------------------------------------


def test_synthesize_includes_quarantine_marker(tmp_path: Path) -> None:
    """SKILL.md contains the quarantine marker comment."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    synth.synthesize(_create_skill_insight())

    content = (tmp_path / "read-then-edit" / "SKILL.md").read_text(encoding="utf-8")
    assert "<!-- generated-by: opencomputer-evolution -->" in content


# ---------------------------------------------------------------------------
# 4. Traceability metadata comments
# ---------------------------------------------------------------------------


def test_synthesize_includes_traceability_metadata(tmp_path: Path) -> None:
    """SKILL.md contains evolution-slug, evolution-confidence, and evolution-evidence-refs."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    synth.synthesize(_create_skill_insight())

    content = (tmp_path / "read-then-edit" / "SKILL.md").read_text(encoding="utf-8")
    assert "<!-- evolution-slug: read-then-edit -->" in content
    assert "<!-- evolution-confidence: 0.85 -->" in content
    assert "<!-- evolution-evidence-refs: 1,2,3 -->" in content


# ---------------------------------------------------------------------------
# 5. Rejects non-create_skill action_type
# ---------------------------------------------------------------------------


def test_synthesize_rejects_non_create_skill_action(tmp_path: Path) -> None:
    """synthesize() raises ValueError for action_type != 'create_skill'."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    insight = _create_skill_insight(action_type="noop", payload_overrides={})
    with pytest.raises(ValueError, match="action_type"):
        synth.synthesize(insight)


# ---------------------------------------------------------------------------
# 6. Rejects missing required payload field
# ---------------------------------------------------------------------------


def test_synthesize_rejects_missing_payload_field(tmp_path: Path) -> None:
    """synthesize() raises ValueError when payload is missing 'body'."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    insight = _create_skill_insight(payload_overrides={"body": None})
    # Remove body entirely from the payload
    payload = dict(insight.payload)
    del payload["body"]
    insight2 = Insight(
        observation=insight.observation,
        evidence_refs=insight.evidence_refs,
        action_type=insight.action_type,
        payload=payload,
        confidence=insight.confidence,
    )
    with pytest.raises(ValueError, match="body"):
        synth.synthesize(insight2)


# ---------------------------------------------------------------------------
# 7. Rejects invalid slug
# ---------------------------------------------------------------------------


def test_synthesize_rejects_invalid_slug(tmp_path: Path) -> None:
    """synthesize() raises ValueError for slug that fails the regex."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    insight = _create_skill_insight(payload_overrides={"slug": "Has Capitals!"})
    with pytest.raises(ValueError, match="slug"):
        synth.synthesize(insight)


# ---------------------------------------------------------------------------
# 8. Writes references/
# ---------------------------------------------------------------------------


def test_synthesize_writes_references(tmp_path: Path) -> None:
    """synthesize() creates references/ dir and writes each reference file."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    insight = _create_skill_insight(
        payload_overrides={
            "references": [
                {"name": "ref1.md", "content": "# Ref 1\n\nSome reference."},
                {"name": "ref2.md", "content": "# Ref 2\n\nAnother reference."},
            ]
        }
    )
    synth.synthesize(insight)

    ref_dir = tmp_path / "read-then-edit" / "references"
    assert ref_dir.is_dir()
    assert (ref_dir / "ref1.md").read_text(encoding="utf-8") == "# Ref 1\n\nSome reference."
    assert (ref_dir / "ref2.md").read_text(encoding="utf-8") == "# Ref 2\n\nAnother reference."


# ---------------------------------------------------------------------------
# 9. Writes examples/
# ---------------------------------------------------------------------------


def test_synthesize_writes_examples(tmp_path: Path) -> None:
    """synthesize() creates examples/ dir and writes each example file."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    insight = _create_skill_insight(
        payload_overrides={
            "examples": [
                {"name": "ex1.md", "content": "Example 1 content."},
                {"name": "ex2.md", "content": "Example 2 content."},
            ]
        }
    )
    synth.synthesize(insight)

    ex_dir = tmp_path / "read-then-edit" / "examples"
    assert ex_dir.is_dir()
    assert (ex_dir / "ex1.md").read_text(encoding="utf-8") == "Example 1 content."
    assert (ex_dir / "ex2.md").read_text(encoding="utf-8") == "Example 2 content."


# ---------------------------------------------------------------------------
# 10. Omits optional dirs when payload has no references/examples
# ---------------------------------------------------------------------------


def test_synthesize_omits_optional_dirs_when_empty(tmp_path: Path) -> None:
    """synthesize() does not create references/ or examples/ when not in payload."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    synth.synthesize(_create_skill_insight())

    skill_dir = tmp_path / "read-then-edit"
    assert not (skill_dir / "references").exists()
    assert not (skill_dir / "examples").exists()
    assert (skill_dir / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# 11. Rejects path-traversal in reference name
# ---------------------------------------------------------------------------


def test_synthesize_rejects_path_traversal_in_reference_name(tmp_path: Path) -> None:
    """synthesize() raises ValueError for path-traversal reference names."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    insight = _create_skill_insight(
        payload_overrides={
            "references": [
                {"name": "../etc/passwd", "content": "x"},
            ]
        }
    )
    with pytest.raises(ValueError, match="unsafe"):
        synth.synthesize(insight)

    # The file outside dest_dir must NOT have been written
    assert not (tmp_path / "etc" / "passwd").exists()


# ---------------------------------------------------------------------------
# 12. Handles slug collision — second call gets -2 suffix
# ---------------------------------------------------------------------------


def test_synthesize_handles_slug_collision(tmp_path: Path) -> None:
    """Second synthesize() with same slug appends -2."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    first = synth.synthesize(_create_skill_insight())
    second = synth.synthesize(_create_skill_insight())

    assert first == tmp_path / "read-then-edit"
    assert second == tmp_path / "read-then-edit-2"
    assert first.is_dir()
    assert second.is_dir()


# ---------------------------------------------------------------------------
# 13. Handles multiple collisions — third call gets -3
# ---------------------------------------------------------------------------


def test_synthesize_handles_multiple_collisions(tmp_path: Path) -> None:
    """Three synthesize() calls with same slug yield -1 (base), -2, -3."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    first = synth.synthesize(_create_skill_insight())
    second = synth.synthesize(_create_skill_insight())
    third = synth.synthesize(_create_skill_insight())

    assert first == tmp_path / "read-then-edit"
    assert second == tmp_path / "read-then-edit-2"
    assert third == tmp_path / "read-then-edit-3"
    assert all(p.is_dir() for p in [first, second, third])


# ---------------------------------------------------------------------------
# 14. Atomic write — no partial dir left on failure
# ---------------------------------------------------------------------------


def test_synthesize_atomic_write_no_partial_dir_on_failure(tmp_path: Path) -> None:
    """On write failure mid-tree, the final skill dir does not exist and no tmp dirs linger."""
    call_count = 0
    original = SkillSynthesizer._write_safe_named_file

    def _fail_on_second(parent: Path, name: str, content: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise RuntimeError("injected failure on second file write")
        original(parent, name, content)

    insight = _create_skill_insight(
        payload_overrides={
            "references": [
                {"name": "ref1.md", "content": "First reference."},
                {"name": "ref2.md", "content": "Second reference."},
            ]
        }
    )

    synth = SkillSynthesizer(dest_dir=tmp_path)
    with patch.object(SkillSynthesizer, "_write_safe_named_file", staticmethod(_fail_on_second)):
        with pytest.raises(RuntimeError, match="injected failure"):
            synth.synthesize(insight)

    # Final dir must NOT exist (atomic write rolled back)
    assert not (tmp_path / "read-then-edit").exists()

    # No tmp dirs should linger
    lingering = [p for p in tmp_path.iterdir() if p.name.startswith(".read-then-edit.tmp.")]
    assert lingering == [], f"Lingering tmp dirs found: {lingering}"


# ---------------------------------------------------------------------------
# 15. Default dest_dir uses evolution_home / skills
# ---------------------------------------------------------------------------


def test_synthesize_default_dest_dir_uses_evolution_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without explicit dest_dir, skill lands in <OPENCOMPUTER_HOME>/evolution/skills/<slug>/."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    synth = SkillSynthesizer()  # no dest_dir
    result = synth.synthesize(_create_skill_insight())

    expected = tmp_path / "evolution" / "skills" / "read-then-edit"
    assert result == expected
    assert (expected / "SKILL.md").exists()
