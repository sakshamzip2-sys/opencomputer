"""Direct tests for QuarantineWriter (Phase 5.B-1)."""

from __future__ import annotations

import pytest

from opencomputer.evolution.quarantine_writer import (
    QuarantinedSkill,
    QuarantineWriter,
)

_BODY = "---\nname: foo\ndescription: x\n---\n# Foo body"


def _writer(tmp_path):
    return QuarantineWriter(dest_root=tmp_path / "skills")


# ---------- Slug validation ----------


def test_slug_must_match_pattern(tmp_path):
    w = _writer(tmp_path)
    skill = QuarantinedSkill(slug="Bad_Slug", skill_md_content=_BODY)
    with pytest.raises(ValueError, match="must match"):
        w.write(skill)


def test_slug_starts_with_alphanumeric(tmp_path):
    w = _writer(tmp_path)
    with pytest.raises(ValueError):
        w.write(QuarantinedSkill(slug="-leading", skill_md_content=_BODY))


# ---------- Atomic write ----------


def test_writes_skill_md(tmp_path):
    w = _writer(tmp_path)
    skill = QuarantinedSkill(slug="alpha", skill_md_content=_BODY)
    out = w.write(skill)
    assert out == tmp_path / "skills" / "alpha"
    assert (out / "SKILL.md").read_text() == _BODY


def test_writes_creates_dest_root(tmp_path):
    """dest_root is created on demand so callers don't have to pre-mkdir."""
    w = QuarantineWriter(dest_root=tmp_path / "fresh" / "deep" / "skills")
    out = w.write(QuarantinedSkill(slug="x", skill_md_content=_BODY))
    assert out.is_dir()


# ---------- Slug auto-collision ----------


def test_collision_appends_dash_2(tmp_path):
    w = _writer(tmp_path)
    w.write(QuarantinedSkill(slug="alpha", skill_md_content=_BODY))
    out2 = w.write(QuarantinedSkill(slug="alpha", skill_md_content=_BODY))
    assert out2.name == "alpha-2"


def test_collision_walks_until_free(tmp_path):
    w = _writer(tmp_path)
    for _ in range(5):
        w.write(QuarantinedSkill(slug="beta", skill_md_content=_BODY))
    listing = sorted((tmp_path / "skills").iterdir())
    names = {p.name for p in listing}
    assert names == {"beta", "beta-2", "beta-3", "beta-4", "beta-5"}


# ---------- References + examples ----------


def test_references_written(tmp_path):
    w = _writer(tmp_path)
    skill = QuarantinedSkill(
        slug="r1",
        skill_md_content=_BODY,
        references=({"name": "ref.md", "content": "reference"},),
    )
    out = w.write(skill)
    assert (out / "references" / "ref.md").read_text() == "reference"


def test_examples_written(tmp_path):
    w = _writer(tmp_path)
    skill = QuarantinedSkill(
        slug="e1",
        skill_md_content=_BODY,
        examples=({"name": "ex.py", "content": "print(1)"},),
    )
    out = w.write(skill)
    assert (out / "examples" / "ex.py").read_text() == "print(1)"


def test_unsafe_reference_name_rejected(tmp_path):
    w = _writer(tmp_path)
    for unsafe in ("../escape", "/abs", ".hidden", ""):
        skill = QuarantinedSkill(
            slug="rejecty",
            skill_md_content=_BODY,
            references=({"name": unsafe, "content": "x"},),
        )
        with pytest.raises(ValueError):
            w.write(skill)


def test_partial_write_cleaned_up_on_error(tmp_path):
    """If a reference fails mid-write, no partial dir is left behind."""
    w = _writer(tmp_path)
    skill = QuarantinedSkill(
        slug="atomic",
        skill_md_content=_BODY,
        references=(
            {"name": "good.md", "content": "ok"},
            {"name": "../bad", "content": "evil"},
        ),
    )
    with pytest.raises(ValueError):
        w.write(skill)
    # Final dir was never created
    assert not (tmp_path / "skills" / "atomic").exists()
    # No leftover tmp dirs either (mkdtemp prefix is .atomic.tmp.)
    leftover = list((tmp_path / "skills").glob(".atomic.tmp.*"))
    assert leftover == []


def test_invalid_reference_shape_rejected(tmp_path):
    w = _writer(tmp_path)
    skill = QuarantinedSkill(
        slug="badshape",
        skill_md_content=_BODY,
        references=({"missing-content": "ref.md"},),
    )
    with pytest.raises(ValueError):
        w.write(skill)
