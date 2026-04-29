"""Tests for agentskills.io-compatible SKILL.md frontmatter validation."""
import pytest

from opencomputer.skills_hub.agentskills_validator import (
    ValidationError,
    validate_frontmatter,
)

VALID = """---
name: pead-screener
description: Screen post-earnings gap-up stocks for PEAD setups
version: 1.0.0
author: saksham
tags: [finance, screening]
---

# PEAD Screener
"""


def test_valid_frontmatter_passes():
    parsed = validate_frontmatter(VALID)
    assert parsed["name"] == "pead-screener"
    assert parsed["description"].startswith("Screen")
    assert parsed["version"] == "1.0.0"
    assert parsed["tags"] == ["finance", "screening"]


def test_missing_name_fails():
    body = "---\ndescription: A reasonable length description here\n---\n# X"
    with pytest.raises(ValidationError, match="missing required field 'name'"):
        validate_frontmatter(body)


def test_missing_description_fails():
    body = "---\nname: foo\n---\n# X"
    with pytest.raises(ValidationError, match="missing required field 'description'"):
        validate_frontmatter(body)


def test_name_must_be_kebab_case():
    body = "---\nname: PEAD_Screener\ndescription: A reasonable length description here\n---"
    with pytest.raises(ValidationError, match="kebab-case"):
        validate_frontmatter(body)


def test_description_too_short_fails():
    body = "---\nname: foo\ndescription: short\n---"
    with pytest.raises(ValidationError, match="description.*at least"):
        validate_frontmatter(body)


def test_description_too_long_fails():
    body = f"---\nname: foo\ndescription: {'a' * 600}\n---"
    with pytest.raises(ValidationError, match="description.*at most"):
        validate_frontmatter(body)


def test_invalid_version_fails():
    body = "---\nname: foo\ndescription: a valid description here please for tests\nversion: not-semver\n---"
    with pytest.raises(ValidationError, match="version.*semver"):
        validate_frontmatter(body)


def test_no_frontmatter_fails():
    body = "# Just a heading"
    with pytest.raises(ValidationError, match="no frontmatter"):
        validate_frontmatter(body)


def test_unclosed_frontmatter_fails():
    body = "---\nname: foo\ndescription: ok valid here for testing purposes\n# never closes"
    with pytest.raises(ValidationError, match="unclosed frontmatter"):
        validate_frontmatter(body)


def test_tags_must_be_list_of_strings():
    body = "---\nname: foo\ndescription: a valid description here please for tests\ntags: [1, 2, 3]\n---"
    with pytest.raises(ValidationError, match="tags must be a list of strings"):
        validate_frontmatter(body)


def test_kebab_case_with_digits_passes():
    body = "---\nname: skill-v2\ndescription: a valid description here please for tests\n---"
    parsed = validate_frontmatter(body)
    assert parsed["name"] == "skill-v2"


def test_semver_with_prerelease_passes():
    body = "---\nname: foo\ndescription: a valid description here please for tests\nversion: 1.0.0-beta.1\n---"
    parsed = validate_frontmatter(body)
    assert parsed["version"] == "1.0.0-beta.1"
