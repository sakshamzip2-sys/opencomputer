"""Tests for agentskills.io-compatible SKILL.md frontmatter validation."""
import tempfile
from pathlib import Path

import pytest

from opencomputer.skills_hub.agentskills_validator import (
    ValidationError,
    ValidationIssue,
    ValidationReport,
    validate_frontmatter,
    validate_skill_dir,
    validate_skill_md,
)


def test_validation_report_has_errors_and_warnings():
    issue_err = ValidationIssue(
        rule="name.reserved_word",
        severity="error",
        field="frontmatter.name",
        message="reserved word",
        line=2,
    )
    issue_warn = ValidationIssue(
        rule="body.size_warn",
        severity="warning",
        field=None,
        message="body exceeds 500 lines",
        line=None,
    )
    report = ValidationReport(
        errors=[issue_err],
        warnings=[issue_warn],
        skill_path=None,
    )
    assert report.errors == [issue_err]
    assert report.warnings == [issue_warn]
    assert report.skill_path is None


def test_validate_skill_md_returns_report_with_existing_validations():
    bad_text = """---
name: BadName
description: short
---

Body content.
"""
    report = validate_skill_md(bad_text, strict=True)
    assert isinstance(report, ValidationReport)
    # Existing validator caught: name not lowercase, description too short (<20 chars)
    assert any(i.field and "name" in i.field for i in report.errors) or any(
        "name" in i.message.lower() or "description" in i.message.lower()
        for i in report.errors
    )


def test_validate_skill_md_clean_report_for_valid_input():
    good_text = """---
name: processing-pdfs
description: Processes PDF files and extracts text. Use when working with PDF documents or extraction tasks.
version: 0.1.0
---

# Processing PDFs

Body content here.
"""
    report = validate_skill_md(good_text, strict=True)
    assert report.passes_strict, f"unexpected issues: {report.errors + report.warnings}"


@pytest.mark.parametrize("name", [
    "anthropic",
    "claude",
    "anthropic-helper",
    "claude-tools",
])
def test_name_reserved_word_blocks(name):
    text = f"""---
name: {name}
description: A reasonable description that's at least twenty chars long for the existing validator.
version: 0.1.0
---

Body.
"""
    report = validate_skill_md(text, strict=True)
    assert any(i.rule == "name.reserved_word" for i in report.errors), \
        f"expected reserved-word error for {name!r}, got: {report.errors}"


@pytest.mark.parametrize("name", [
    "processing-pdfs",
    "analyzing-data",
    "my-skill",
    "github-helper",
    "using-claude-code",
    "with-anthropic-tools",
    "my-anthropic-skill",
])
def test_name_reserved_word_allows_normal_names(name):
    text = f"""---
name: {name}
description: A reasonable description that's at least twenty chars long for the existing validator.
version: 0.1.0
---

Body.
"""
    report = validate_skill_md(text, strict=True)
    assert not any(i.rule == "name.reserved_word" for i in report.errors), \
        f"unexpected reserved-word error for {name!r}: {report.errors}"


def test_xml_tag_in_name_blocks():
    text = """---
name: foo<script>alert
description: A reasonable description that's at least twenty chars long for the existing validator.
version: 0.1.0
---

Body.
"""
    report = validate_skill_md(text, strict=True)
    assert any(i.rule == "name.xml_tag" for i in report.errors), \
        f"expected xml_tag error, got: {report.errors}"


def test_xml_tag_in_description_blocks():
    text = """---
name: my-skill
description: Process files <script>alert(1)</script> and stuff that's long enough.
version: 0.1.0
---

Body.
"""
    report = validate_skill_md(text, strict=True)
    assert any(i.rule == "description.xml_tag" for i in report.errors), \
        f"expected xml_tag error, got: {report.errors}"


def test_xml_tag_allows_normal_punctuation():
    text = """---
name: my-skill
description: Processes JSON and YAML data with key=value semantics. Use when needed.
version: 0.1.0
---

Body.
"""
    report = validate_skill_md(text, strict=True)
    assert not any(i.rule.endswith("xml_tag") for i in report.errors)


@pytest.mark.parametrize("description", [
    "I can help you process files. Use when working with documents.",
    "You can use this to extract text. Use when reading PDFs.",
    "We help analyze data. Use when reviewing spreadsheets.",
    "Let me explain JSON parsing. Use when working with structured data.",
    "I'll process your input. Use when handling user uploads.",
    "This helps you query BigQuery. Use when analyzing warehouse data.",
])
def test_description_voice_warning_for_first_or_second_person(description):
    text = f"""---
name: my-skill
description: {description}
version: 0.1.0
---

Body.
"""
    report = validate_skill_md(text, strict=True)
    assert any(i.rule == "description.voice" for i in report.warnings), \
        f"expected voice warning for {description!r}, got warnings: {report.warnings}"


@pytest.mark.parametrize("description", [
    "Processes PDF files and extracts text. Use when working with PDFs.",
    "Synthesizes git commit messages from staged diffs. Use when committing.",
    "Generates compliance reports. Use when auditing data.",
    "Analyzes spreadsheet data. Use when working with Excel files.",
])
def test_description_voice_allows_third_person(description):
    text = f"""---
name: my-skill
description: {description}
version: 0.1.0
---

Body.
"""
    report = validate_skill_md(text, strict=True)
    assert not any(i.rule == "description.voice" for i in report.warnings), \
        f"unexpected voice warning for {description!r}: {report.warnings}"


def test_description_voice_allows_pronouns_inside_code_spans():
    text = """---
name: my-skill
description: Processes documents marked with `you` or `I` annotations. Use when reviewing.
version: 0.1.0
---

Body.
"""
    report = validate_skill_md(text, strict=True)
    assert not any(i.rule == "description.voice" for i in report.warnings)


def test_body_size_warning_over_500_lines():
    body = "Line.\n" * 600
    text = f"""---
name: big-skill
description: A description that's long enough for the existing validator. Use when needed.
version: 0.1.0
---

{body}"""
    report = validate_skill_md(text, strict=True)
    assert any(i.rule == "body.size_warn" for i in report.warnings)


def test_body_size_no_warning_under_500_lines():
    body = "Line.\n" * 400
    text = f"""---
name: small-skill
description: A description that's long enough for the existing validator. Use when needed.
version: 0.1.0
---

{body}"""
    report = validate_skill_md(text, strict=True)
    assert not any(i.rule == "body.size_warn" for i in report.warnings)


def test_body_size_exempt_when_review_date_present():
    body = "Line.\n" * 600
    text = f"""---
name: big-but-reviewed
description: A description that's long enough for the existing validator. Use when needed.
version: 0.1.0
size_review_date: 2026-05-02
---

{body}"""
    report = validate_skill_md(text, strict=True)
    assert not any(i.rule == "body.size_warn" for i in report.warnings)


def test_validate_skill_dir_reads_skill_md():
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: processing-pdfs
description: Processes PDF files and extracts text. Use when working with PDFs.
version: 0.1.0
---

Body.
""")
        report = validate_skill_dir(skill_dir, strict=True)
        assert report.passes_strict
        assert report.skill_path == skill_dir / "SKILL.md"


def test_validate_skill_dir_missing_skill_md_errors():
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "empty-skill"
        skill_dir.mkdir()
        report = validate_skill_dir(skill_dir, strict=True)
        assert any(i.rule == "skill_md.missing" for i in report.errors)


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


def test_skill_manage_uses_unified_validator():
    """Regression: skill_manage._validate_frontmatter must delegate to the
    unified hub validator so all write paths share one contract.

    Verifies by feeding inputs that ONLY the unified validator would reject:
    a non-kebab-case name and a too-short description. The legacy ad-hoc
    checks in skill_manage previously accepted both.
    """
    from opencomputer.tools.skill_manage import _validate_frontmatter

    # Non-kebab-case name (single char fails the unified NAME_RE pattern).
    bad_name = "---\nname: x\ndescription: hello\n---\nbody\n"
    err_name = _validate_frontmatter(bad_name)
    assert err_name is not None, "expected unified validator to reject single-char name"
    assert "kebab-case" in err_name or "name" in err_name.lower()

    # Description under the 20-char minimum.
    bad_desc = "---\nname: my-skill\ndescription: short\n---\nbody\n"
    err_desc = _validate_frontmatter(bad_desc)
    assert err_desc is not None, "expected unified validator to reject short description"
    assert "description" in err_desc.lower() or "20" in err_desc

    # And valid input still returns None.
    good = (
        "---\n"
        "name: my-skill\n"
        "description: A valid description that is at least twenty chars long.\n"
        "---\n"
        "body\n"
    )
    assert _validate_frontmatter(good) is None
