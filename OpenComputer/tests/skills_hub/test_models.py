"""Tests for SkillSource public ABC and dataclasses."""
import pytest

from plugin_sdk.skill_source import SkillBundle, SkillMeta, SkillSource


def test_skill_meta_required_fields():
    meta = SkillMeta(
        identifier="well-known/pead-screener",
        name="pead-screener",
        description="Screen post-earnings gap-up stocks",
        source="well-known",
    )
    assert meta.identifier == "well-known/pead-screener"
    assert meta.trust_level == "community"


def test_skill_meta_optional_fields():
    meta = SkillMeta(
        identifier="well-known/foo",
        name="foo",
        description="bar",
        source="well-known",
        version="1.2.0",
        author="alice",
        tags=("finance", "screening"),
        trust_level="trusted",
    )
    assert meta.version == "1.2.0"
    assert meta.author == "alice"
    assert "finance" in meta.tags
    assert meta.trust_level == "trusted"


def test_skill_bundle_with_skill_md_required():
    bundle = SkillBundle(
        identifier="well-known/foo",
        skill_md="---\nname: foo\ndescription: bar\n---\n# Foo",
        files={},
    )
    assert "name: foo" in bundle.skill_md


def test_skill_bundle_with_extra_files():
    bundle = SkillBundle(
        identifier="well-known/foo",
        skill_md="---\nname: foo\ndescription: bar\n---",
        files={"helper.py": "def x(): pass\n"},
    )
    assert bundle.files["helper.py"].startswith("def x")


def test_trust_level_must_be_valid():
    with pytest.raises(ValueError):
        SkillMeta(
            identifier="x", name="x", description="y", source="z",
            trust_level="invalid_value",
        )


def test_skill_source_is_abstract():
    """Cannot instantiate the ABC directly."""
    with pytest.raises(TypeError):
        SkillSource()  # type: ignore[abstract]


def test_skill_source_concrete_subclass_works():
    """A minimal concrete subclass can be instantiated."""
    class _Concrete(SkillSource):
        @property
        def name(self) -> str:
            return "concrete"

        def search(self, query: str, limit: int = 10):
            return []

        def fetch(self, identifier: str):
            return None

        def inspect(self, identifier: str):
            return None

    src = _Concrete()
    assert src.name == "concrete"
    assert src.search("foo") == []
