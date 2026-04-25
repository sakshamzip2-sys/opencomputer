"""PR-5: tests for constraint gates on synthesized skills."""
from __future__ import annotations

import pytest

from opencomputer.evolution.constraints import (
    MAX_DESCRIPTION_LEN,
    MAX_EXAMPLE_FILES,
    MAX_REF_FILE_SIZE_BYTES,
    MAX_REFERENCE_FILES,
    MAX_SKILL_SIZE_BYTES,
    MIN_BODY_LEN,
    ConstraintViolation,
    validate_synthesized_skill,
)


def _good_payload(**overrides):
    base = {
        "slug": "read-then-edit",
        "name": "Read-then-Edit",
        "description": "Use after reading a file when an edit is intended.",
        "body": "# Read-then-Edit\n\nReads a file then immediately edits it. " * 5,
    }
    base.update(overrides)
    return base


def test_happy_path_passes():
    validate_synthesized_skill(_good_payload())


def test_missing_slug_fails():
    p = _good_payload()
    p.pop("slug")
    with pytest.raises(ConstraintViolation, match="slug"):
        validate_synthesized_skill(p)


def test_invalid_slug_uppercase_fails():
    with pytest.raises(ConstraintViolation, match="slug"):
        validate_synthesized_skill(_good_payload(slug="ReadThenEdit"))


def test_invalid_slug_starts_with_hyphen_fails():
    with pytest.raises(ConstraintViolation, match="slug"):
        validate_synthesized_skill(_good_payload(slug="-leading-hyphen"))


def test_slug_too_long_fails():
    with pytest.raises(ConstraintViolation, match="slug"):
        validate_synthesized_skill(_good_payload(slug="a" * 51))


def test_missing_name_fails():
    p = _good_payload()
    p.pop("name")
    with pytest.raises(ConstraintViolation, match="name"):
        validate_synthesized_skill(p)


def test_empty_name_fails():
    with pytest.raises(ConstraintViolation, match="name"):
        validate_synthesized_skill(_good_payload(name="   "))


def test_body_too_short_fails():
    with pytest.raises(ConstraintViolation, match="body too short"):
        validate_synthesized_skill(_good_payload(body="x" * (MIN_BODY_LEN - 1)))


def test_body_too_large_fails():
    big = "x" * (MAX_SKILL_SIZE_BYTES + 1)
    with pytest.raises(ConstraintViolation, match="exceeds"):
        validate_synthesized_skill(_good_payload(body=big))


def test_body_at_max_size_passes():
    body = "x" * MAX_SKILL_SIZE_BYTES
    validate_synthesized_skill(_good_payload(body=body))


def test_body_with_path_traversal_fails():
    body = "Open ../../etc/passwd to learn things " * 3
    with pytest.raises(ConstraintViolation, match="path.traversal"):
        validate_synthesized_skill(_good_payload(body=body))


def test_description_too_long_fails():
    with pytest.raises(ConstraintViolation, match="description"):
        validate_synthesized_skill(_good_payload(description="x" * (MAX_DESCRIPTION_LEN + 1)))


def test_too_many_references_fails():
    refs = [{"name": f"r{i}.md", "content": "x"} for i in range(MAX_REFERENCE_FILES + 1)]
    with pytest.raises(ConstraintViolation, match="reference"):
        validate_synthesized_skill(_good_payload(references=refs))


def test_too_many_examples_fails():
    exs = [{"name": f"e{i}.md", "content": "x"} for i in range(MAX_EXAMPLE_FILES + 1)]
    with pytest.raises(ConstraintViolation, match="example"):
        validate_synthesized_skill(_good_payload(examples=exs))


def test_oversize_reference_content_fails():
    refs = [{"name": "big.md", "content": "x" * (MAX_REF_FILE_SIZE_BYTES + 1)}]
    with pytest.raises(ConstraintViolation, match="exceeds"):
        validate_synthesized_skill(_good_payload(references=refs))


def test_constraint_violation_is_value_error():
    """ConstraintViolation MUST be a ValueError subclass so existing
    catch (ValueError, FileExistsError) handlers still work."""
    assert issubclass(ConstraintViolation, ValueError)


def test_validate_called_by_synthesize_blocks_oversize(tmp_path, monkeypatch):
    """End-to-end: SkillSynthesizer.synthesize delegates to validate_synthesized_skill."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.evolution.reflect import Insight
    from opencomputer.evolution.synthesize import SkillSynthesizer
    insight = Insight(
        observation="repeated pattern",
        evidence_refs=(1, 2),
        action_type="create_skill",
        payload=_good_payload(body="x" * (MAX_SKILL_SIZE_BYTES + 1)),
        confidence=0.9,
    )
    synth = SkillSynthesizer()
    with pytest.raises(ConstraintViolation):
        synth.synthesize(insight)
    # And no skill dir was created
    assert not (tmp_path / "evolution" / "skills" / "read-then-edit").exists()
