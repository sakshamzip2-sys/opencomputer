"""Tests for the bundled well-known SkillSource."""
import json
from pathlib import Path

import pytest

from opencomputer.skills_hub.sources.well_known import WellKnownSource


@pytest.fixture
def fake_manifest(tmp_path) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({
        "version": 1,
        "entries": [
            {
                "identifier": "well-known/foo-bar",
                "name": "foo-bar",
                "description": "An example foo-bar skill for testing search behavior",
                "version": "1.0.0",
                "trust_level": "trusted",
                "skill_md": "---\nname: foo-bar\ndescription: An example foo-bar skill for testing search behavior\n---\n# Foo",
                "files": {},
            },
            {
                "identifier": "well-known/baz",
                "name": "baz",
                "description": "Different skill for non-matching search test verification",
                "version": "0.1.0",
                "trust_level": "community",
                "skill_md": "---\nname: baz\ndescription: Different skill for non-matching search test verification\n---\n# Baz",
                "files": {},
            },
        ],
    }))
    return p


def test_source_name_is_well_known(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    assert src.name == "well-known"


def test_search_substring_match_returns_meta(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    results = src.search("foo")
    assert len(results) == 1
    assert results[0].name == "foo-bar"


def test_search_returns_empty_when_no_match(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    assert src.search("nonexistent-xyzzy") == []


def test_search_respects_limit(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    results = src.search("", limit=1)
    assert len(results) == 1


def test_inspect_returns_meta_for_known(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    meta = src.inspect("well-known/foo-bar")
    assert meta is not None
    assert meta.name == "foo-bar"
    assert meta.version == "1.0.0"


def test_inspect_returns_none_for_unknown(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    assert src.inspect("well-known/nope") is None


def test_fetch_returns_bundle_with_skill_md(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    bundle = src.fetch("well-known/foo-bar")
    assert bundle is not None
    assert "name: foo-bar" in bundle.skill_md


def test_fetch_returns_none_for_unknown(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    assert src.fetch("well-known/nope") is None


def test_default_manifest_path_loads_bundled():
    """No path argument falls back to the bundled manifest in the package."""
    src = WellKnownSource()
    # Default bundled has at least one entry
    assert len(src.search("", limit=10)) >= 1


def test_bundled_manifest_entries_pass_validator():
    """Every bundled well-known entry must satisfy the agentskills.io validator."""
    from opencomputer.skills_hub.agentskills_validator import validate_frontmatter

    src = WellKnownSource()
    for meta in src.search("", limit=100):
        bundle = src.fetch(meta.identifier)
        assert bundle is not None, f"missing bundle for {meta.identifier}"
        # Should not raise
        validate_frontmatter(bundle.skill_md)
