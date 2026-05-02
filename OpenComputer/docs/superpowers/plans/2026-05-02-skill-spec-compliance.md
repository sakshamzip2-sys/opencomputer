# Skill Spec Compliance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring OpenComputer's skill subsystem into compliance with Anthropic's published Agent Skills spec — unify the two validators, add 4 missing frontmatter checks, fix the auto-skill-evo synthesis prompt's voice bug, and audit the 14 over-500-line bundled skills.

**Architecture:** Two-tier enforcement (errors block always, warnings only block in `strict=True`). Composition over replacement: `skill_manage.py` delegates to a single source-of-truth validator in `agentskills_validator.py`. Synthesis prompt rewritten to teach 3rd-person + WHAT+WHEN voice. Bundled corpus split-or-exempt with documented `size_review_date` frontmatter.

**Tech Stack:** Python 3.12+, pytest, Jinja2 (synthesis prompt), pydantic-style dataclasses.

**Spec:** [`docs/superpowers/specs/2026-05-02-skill-spec-compliance-design.md`](../specs/2026-05-02-skill-spec-compliance-design.md)

---

## Pre-flight

- [ ] **Step 0a: Verify base branch + worktree**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git status
git branch --show-current
```

Expected: clean tree, on a feature branch like `feat/skill-spec-compliance` or in a dedicated worktree. If on `main` or an unrelated feature branch, stop and create a worktree:

```bash
git worktree add ../OpenComputer-skill-spec-compliance -b feat/skill-spec-compliance main
cd ../OpenComputer-skill-spec-compliance
```

- [ ] **Step 0b: Establish baseline — run full test suite**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
pytest tests/ -x --tb=short 2>&1 | tail -20
```

Expected: all tests pass (885+ per CLAUDE.md §4). Record the count. Any pre-existing failures must be noted before we start; we don't want to attribute them to this work.

- [ ] **Step 0c: Establish ruff baseline**

```bash
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: clean. Record any pre-existing warnings.

---

## Task 1: ValidationReport + ValidationIssue dataclasses

**Files:**
- Modify: `opencomputer/skills_hub/agentskills_validator.py`
- Test: `tests/skills_hub/test_agentskills_validator.py`

- [ ] **Step 1: Read the existing validator to understand its current shape**

```bash
cat opencomputer/skills_hub/agentskills_validator.py | head -100
```

Note the existing `validate_frontmatter()` signature and current return type. We're adding a richer report layer alongside it (back-compat preserved).

- [ ] **Step 2: Write the failing test**

Add to `tests/skills_hub/test_agentskills_validator.py`:

```python
from opencomputer.skills_hub.agentskills_validator import (
    ValidationIssue,
    ValidationReport,
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
```

- [ ] **Step 3: Run test to verify it fails**

```bash
pytest tests/skills_hub/test_agentskills_validator.py::test_validation_report_has_errors_and_warnings -v
```

Expected: FAIL with `ImportError: cannot import name 'ValidationIssue'`.

- [ ] **Step 4: Add the dataclasses**

Add to top of `opencomputer/skills_hub/agentskills_validator.py` (after existing imports):

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class ValidationIssue:
    """A single validator finding (error or warning)."""
    rule: str                    # e.g. "name.reserved_word"
    severity: Literal["error", "warning"]
    field: str | None            # e.g. "frontmatter.name"
    message: str
    line: int | None = None


@dataclass
class ValidationReport:
    """Result of validating a SKILL.md file or directory."""
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    skill_path: Path | None = None

    @property
    def is_clean(self) -> bool:
        return not self.errors and not self.warnings

    @property
    def passes_strict(self) -> bool:
        """Strict mode: warnings count as failures."""
        return not self.errors and not self.warnings

    @property
    def passes_lenient(self) -> bool:
        """Lenient mode: only errors block."""
        return not self.errors

    def raise_if_errors(self) -> None:
        """Raise ValidationError if any errors are present."""
        if self.errors:
            messages = "; ".join(f"{i.rule}: {i.message}" for i in self.errors)
            raise ValidationError(messages)
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/skills_hub/test_agentskills_validator.py::test_validation_report_has_errors_and_warnings -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/skills_hub/agentskills_validator.py tests/skills_hub/test_agentskills_validator.py
git commit -m "feat(skills_hub): add ValidationReport + ValidationIssue dataclasses"
```

---

## Task 2: validate_skill_md() function — basic shell

**Files:**
- Modify: `opencomputer/skills_hub/agentskills_validator.py`
- Test: `tests/skills_hub/test_agentskills_validator.py`

- [ ] **Step 1: Write the failing test**

```python
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
    assert any("name" in i.field for i in report.errors if i.field)


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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/skills_hub/test_agentskills_validator.py::test_validate_skill_md_returns_report_with_existing_validations tests/skills_hub/test_agentskills_validator.py::test_validate_skill_md_clean_report_for_valid_input -v
```

Expected: FAIL with `NameError: validate_skill_md`.

- [ ] **Step 3: Implement `validate_skill_md`**

Add to `opencomputer/skills_hub/agentskills_validator.py`:

```python
def validate_skill_md(
    text: str,
    *,
    strict: bool = True,
    path: Path | None = None,
) -> ValidationReport:
    """Validate a SKILL.md file's text against the Anthropic spec.

    Args:
        text: The full SKILL.md content (frontmatter + body).
        strict: If True, warnings count as failures via .passes_strict.
        path: Optional path for error reporting.

    Returns:
        ValidationReport with errors and warnings populated.
    """
    report = ValidationReport(skill_path=path)
    frontmatter, body = _split_frontmatter(text)
    if frontmatter is None:
        report.errors.append(ValidationIssue(
            rule="frontmatter.missing",
            severity="error",
            field=None,
            message="no YAML frontmatter found",
        ))
        return report

    # Delegate existing checks: parse frontmatter and run legacy validator.
    # The legacy validator raises on first error; we wrap to collect all.
    try:
        parsed = _parse_yaml(frontmatter)
    except Exception as exc:
        report.errors.append(ValidationIssue(
            rule="frontmatter.parse_error",
            severity="error",
            field=None,
            message=str(exc),
        ))
        return report

    # Existing checks (name regex, description length, version semver, tags type).
    # Convert raises to issues.
    _run_legacy_checks(parsed, report)

    # New checks (added in subsequent tasks).
    _check_name_reserved_word(parsed.get("name", ""), report)
    _check_xml_tags(parsed, report)
    _check_description_voice(parsed.get("description", ""), report)
    _check_body_size(body, parsed, report)

    return report


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split text into (frontmatter_yaml, body). Returns (None, text) if no frontmatter."""
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end == -1:
        return None, text
    fm = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    return fm, body


def _parse_yaml(text: str) -> dict:
    """Parse YAML frontmatter using PyYAML."""
    import yaml
    return yaml.safe_load(text) or {}


def _run_legacy_checks(parsed: dict, report: ValidationReport) -> None:
    """Run the existing validate_frontmatter checks, converting raises to issues."""
    # Stub: subsequent tasks will integrate these.
    # For now, run the existing function and on raise, append an error.
    try:
        validate_frontmatter(parsed)
    except ValidationError as exc:
        report.errors.append(ValidationIssue(
            rule="legacy",
            severity="error",
            field=None,
            message=str(exc),
        ))


def _check_name_reserved_word(name: str, report: ValidationReport) -> None:
    """Stub — implemented in Task 3."""
    pass


def _check_xml_tags(parsed: dict, report: ValidationReport) -> None:
    """Stub — implemented in Task 4."""
    pass


def _check_description_voice(description: str, report: ValidationReport) -> None:
    """Stub — implemented in Task 5."""
    pass


def _check_body_size(body: str, parsed: dict, report: ValidationReport) -> None:
    """Stub — implemented in Task 6."""
    pass
```

If the existing module already imports `yaml` at the top, skip the import-inside-function. Inspect first.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/skills_hub/test_agentskills_validator.py::test_validate_skill_md_returns_report_with_existing_validations tests/skills_hub/test_agentskills_validator.py::test_validate_skill_md_clean_report_for_valid_input -v
```

Expected: PASS.

- [ ] **Step 5: Run full validator test suite**

```bash
pytest tests/skills_hub/test_agentskills_validator.py -v
```

Expected: all tests pass (existing + new). If any existing test fails, fix the integration of the legacy validator before committing.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/skills_hub/agentskills_validator.py tests/skills_hub/test_agentskills_validator.py
git commit -m "feat(skills_hub): add validate_skill_md() with check-stub scaffolding"
```

---

## Task 3: Reserved-word check (name)

**Files:**
- Modify: `opencomputer/skills_hub/agentskills_validator.py:_check_name_reserved_word`
- Test: `tests/skills_hub/test_agentskills_validator.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest

@pytest.mark.parametrize("name", [
    "anthropic",
    "claude",
    "anthropic-helper",
    "claude-tools",
    "my-anthropic-skill",
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/skills_hub/test_agentskills_validator.py -k "reserved_word" -v
```

Expected: FAIL — the stub function does nothing.

- [ ] **Step 3: Implement the check**

Replace the stub in `agentskills_validator.py`:

```python
RESERVED_WORDS = frozenset({"anthropic", "claude"})


def _check_name_reserved_word(name, report: ValidationReport) -> None:
    """Reject skill names containing reserved words (anthropic, claude).

    Coerces non-string YAML values (e.g. `name: 123`) to string so we
    don't crash on malformed frontmatter — that case is caught by the
    legacy validator's name-format check.
    """
    if not name:
        return  # missing name caught by legacy check
    name_lower = str(name).lower()
    for word in RESERVED_WORDS:
        if word in name_lower.split("-") or name_lower == word:
            report.errors.append(ValidationIssue(
                rule="name.reserved_word",
                severity="error",
                field="frontmatter.name",
                message=f"name contains reserved word {word!r}",
            ))
            return
```

Note: we use word-boundary matching via `split("-")` so "philanthropic" wouldn't trigger on "anthropic" (no hyphenation match). But `my-anthropic-skill` does match because "anthropic" is one of the hyphen-separated tokens.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/skills_hub/test_agentskills_validator.py -k "reserved_word" -v
```

Expected: PASS for all parametrize cases.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/skills_hub/agentskills_validator.py tests/skills_hub/test_agentskills_validator.py
git commit -m "feat(skills_hub): add reserved-word check (anthropic, claude)"
```

---

## Task 4: XML-tag check (name + description)

**Files:**
- Modify: `opencomputer/skills_hub/agentskills_validator.py:_check_xml_tags`
- Test: `tests/skills_hub/test_agentskills_validator.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/skills_hub/test_agentskills_validator.py -k "xml_tag" -v
```

Expected: FAIL.

- [ ] **Step 3: Implement the check**

```python
import re

XML_TAG_RE = re.compile(r"<[a-zA-Z!?/]")


def _check_xml_tags(parsed: dict, report: ValidationReport) -> None:
    """Reject XML/HTML tag opens in name or description.

    Coerces values to string defensively (YAML may parse them as int/list).
    """
    name = str(parsed.get("name", "") or "")
    desc = str(parsed.get("description", "") or "")
    if XML_TAG_RE.search(name):
        report.errors.append(ValidationIssue(
            rule="name.xml_tag",
            severity="error",
            field="frontmatter.name",
            message="name contains XML/HTML tag opener",
        ))
    if XML_TAG_RE.search(desc):
        report.errors.append(ValidationIssue(
            rule="description.xml_tag",
            severity="error",
            field="frontmatter.description",
            message="description contains XML/HTML tag opener",
        ))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/skills_hub/test_agentskills_validator.py -k "xml_tag" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/skills_hub/agentskills_validator.py tests/skills_hub/test_agentskills_validator.py
git commit -m "feat(skills_hub): add XML-tag check for name and description"
```

---

## Task 5: Voice deny-list check (description)

**Files:**
- Modify: `opencomputer/skills_hub/agentskills_validator.py:_check_description_voice`
- Test: `tests/skills_hub/test_agentskills_validator.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/skills_hub/test_agentskills_validator.py -k "voice" -v
```

Expected: FAIL — stub does nothing.

- [ ] **Step 3: Implement the check**

```python
VOICE_DENY_RE = re.compile(
    # Anchored at start; pronouns require trailing whitespace so we don't
    # match "I/O" or "Wewerk" by accident. "I'll" / "I'm" handled
    # separately. Multi-word phrases ("Let me", "I can") require a space
    # between tokens.
    r"^\s*(I\s|You\s|We\s|Let\s+me\s|I['’]?ll\s|I['’]?m\s|I\s+can\s|You\s+can\s|This\s+(?:helps|lets)\s+you\s)",
    re.IGNORECASE,
)

CODE_SPAN_RE = re.compile(r"`[^`]*`")


def _check_description_voice(description, report: ValidationReport) -> None:
    """Warn on 1st/2nd-person voice violations in descriptions.

    Anthropic spec requires 3rd-person ("Processes...", "Synthesizes...").
    The check strips inline code spans first so pronouns inside backticks
    don't trigger.

    Coerces value to string defensively.
    """
    if not description:
        return
    desc_str = str(description)
    stripped = CODE_SPAN_RE.sub("", desc_str)
    if VOICE_DENY_RE.match(stripped):
        report.warnings.append(ValidationIssue(
            rule="description.voice",
            severity="warning",
            field="frontmatter.description",
            message=(
                "description starts with 1st/2nd-person voice "
                "(use 3rd-person: 'Processes...', 'Synthesizes...')"
            ),
        ))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/skills_hub/test_agentskills_validator.py -k "voice" -v
```

Expected: PASS for all cases.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/skills_hub/agentskills_validator.py tests/skills_hub/test_agentskills_validator.py
git commit -m "feat(skills_hub): add 3rd-person voice check (warning) for descriptions"
```

---

## Task 6: Body-size check + size_review_date exemption

**Files:**
- Modify: `opencomputer/skills_hub/agentskills_validator.py:_check_body_size`
- Test: `tests/skills_hub/test_agentskills_validator.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/skills_hub/test_agentskills_validator.py -k "body_size" -v
```

Expected: FAIL.

- [ ] **Step 3: Implement the check**

```python
BODY_SIZE_LIMIT = 500


def _check_body_size(body: str, parsed: dict, report: ValidationReport) -> None:
    """Warn if SKILL.md body exceeds 500 lines.

    Suppressed if frontmatter contains `size_review_date: <ISO date>`,
    which indicates a documented exemption.
    """
    if parsed.get("size_review_date"):
        return  # documented exemption
    line_count = body.count("\n")
    if line_count > BODY_SIZE_LIMIT:
        report.warnings.append(ValidationIssue(
            rule="body.size_warn",
            severity="warning",
            field=None,
            message=(
                f"body has {line_count} lines, exceeds {BODY_SIZE_LIMIT}-line "
                "guideline (split into reference files OR add "
                "`size_review_date` frontmatter to document exemption)"
            ),
        ))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/skills_hub/test_agentskills_validator.py -k "body_size" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/skills_hub/agentskills_validator.py tests/skills_hub/test_agentskills_validator.py
git commit -m "feat(skills_hub): add body-size warning + size_review_date exemption"
```

---

## Task 7: validate_skill_dir() function

**Files:**
- Modify: `opencomputer/skills_hub/agentskills_validator.py`
- Test: `tests/skills_hub/test_agentskills_validator.py`

- [ ] **Step 1: Write the failing test**

```python
import tempfile
from pathlib import Path

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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/skills_hub/test_agentskills_validator.py -k "validate_skill_dir" -v
```

Expected: FAIL — `validate_skill_dir` not defined.

- [ ] **Step 3: Implement the function**

```python
def validate_skill_dir(
    skill_dir: Path,
    *,
    strict: bool = True,
) -> ValidationReport:
    """Validate a skill directory by reading and validating its SKILL.md.

    Args:
        skill_dir: Path to the skill directory (contains SKILL.md).
        strict: If True, warnings count as failures via .passes_strict.
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        report = ValidationReport(skill_path=skill_md)
        report.errors.append(ValidationIssue(
            rule="skill_md.missing",
            severity="error",
            field=None,
            message=f"SKILL.md not found in {skill_dir}",
        ))
        return report
    text = skill_md.read_text()
    return validate_skill_md(text, strict=strict, path=skill_md)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/skills_hub/test_agentskills_validator.py -k "validate_skill_dir" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/skills_hub/agentskills_validator.py tests/skills_hub/test_agentskills_validator.py
git commit -m "feat(skills_hub): add validate_skill_dir() entry point"
```

---

## Task 8: Refactor skill_manage.py to delegate

**Files:**
- Modify: `opencomputer/tools/skill_manage.py`
- Test: existing `tests/skills_guard/test_skill_manage_gate.py` (should keep passing)

- [ ] **Step 1: Read the current implementation to understand what to replace**

```bash
sed -n '100,150p' opencomputer/tools/skill_manage.py
```

Find the `_validate_frontmatter` function and its callsite.

- [ ] **Step 2: Write a regression test**

Add to `tests/skills_hub/test_agentskills_validator.py`:

```python
def test_skill_manage_uses_unified_validator(tmp_path):
    """skill_manage should reject reserved-word names via the unified validator."""
    from opencomputer.tools.skill_manage import validate_skill_content

    bad = """---
name: anthropic-helper
description: A reasonable description that's at least twenty chars long. Use when needed.
---

Body.
"""
    with pytest.raises(Exception):  # broad: skill_manage may use its own exception type
        validate_skill_content(bad)
```

(Adjust the import to match the actual public entry point exposed by `skill_manage.py`. If the function is private, use the actual SkillTool class.)

- [ ] **Step 3: Run test to verify it fails**

```bash
pytest tests/skills_hub/test_agentskills_validator.py::test_skill_manage_uses_unified_validator -v
```

Expected: FAIL — current `skill_manage` doesn't check reserved words.

- [ ] **Step 4: Refactor skill_manage.py**

Replace the body of the legacy `_validate_frontmatter` function with:

```python
def _validate_frontmatter(text: str) -> None:
    """Delegates to the unified hub validator. Raises on errors."""
    from opencomputer.skills_hub.agentskills_validator import validate_skill_md
    report = validate_skill_md(text, strict=True)
    report.raise_if_errors()
```

(If the existing function takes parsed YAML rather than raw text, adapt: parse first, then call `validate_skill_md`. The cleanest path is to modify the callsite to pass raw text.)

- [ ] **Step 5: Run regression test + skill_manage tests**

```bash
pytest tests/skills_hub/test_agentskills_validator.py::test_skill_manage_uses_unified_validator tests/skills_guard/ -v
```

Expected: all PASS. If `tests/skills_guard/test_skill_manage_gate.py` fails because the new validator is stricter, audit those tests — they may need updating to reflect the unified contract.

- [ ] **Step 6: Run full pytest suite**

```bash
pytest tests/ -x --tb=short 2>&1 | tail -10
```

Expected: same or better than the baseline from Step 0b.

- [ ] **Step 7: Commit**

```bash
git add opencomputer/tools/skill_manage.py tests/skills_hub/test_agentskills_validator.py
git commit -m "refactor(skill_manage): delegate frontmatter validation to unified hub validator"
```

---

## Task 9: Bundled corpus compliance test

**Files:**
- Create: `tests/skills_hub/test_bundled_corpus_compliance.py`

- [ ] **Step 1: Write the test**

```python
"""Regression test: every bundled SKILL.md passes the unified validator's lenient mode.

Lenient mode (strict=False) means warnings are reported but don't fail.
Errors always fail. This guards against regressions where someone adds a
new bundled skill containing reserved words, XML tags, or malformed
frontmatter.
"""
from pathlib import Path

import pytest

from opencomputer.skills_hub.agentskills_validator import validate_skill_dir


SKILLS_ROOT = Path(__file__).parent.parent.parent / "opencomputer" / "skills"


def _all_skill_dirs() -> list[Path]:
    """Top-level bundled skills only.

    Uses non-recursive glob so reference SKILL.md files inside split skills
    (e.g. opencomputer/skills/research-paper-writing/references/...) are
    not validated as standalone skills.
    """
    return [p.parent for p in SKILLS_ROOT.glob("*/SKILL.md")]


@pytest.mark.parametrize("skill_dir", _all_skill_dirs(), ids=lambda p: p.name)
def test_bundled_skill_has_no_validation_errors(skill_dir):
    report = validate_skill_dir(skill_dir, strict=False)
    error_msgs = [f"{i.rule}: {i.message}" for i in report.errors]
    assert not report.errors, (
        f"{skill_dir.name} has validation errors:\n  " + "\n  ".join(error_msgs)
    )


def test_bundled_corpus_warning_summary(capsys):
    """Print warning summary for visibility (does not fail)."""
    counts: dict[str, int] = {}
    for skill_dir in _all_skill_dirs():
        report = validate_skill_dir(skill_dir, strict=False)
        for issue in report.warnings:
            counts[issue.rule] = counts.get(issue.rule, 0) + 1
    if counts:
        print("\nBundled corpus warning counts:")
        for rule, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {rule}: {count}")
```

- [ ] **Step 2: Run the test to see what's broken**

```bash
pytest tests/skills_hub/test_bundled_corpus_compliance.py -v 2>&1 | tail -50
```

Expected: most tests PASS, some may FAIL with hard errors. Capture the list of failing skills.

- [ ] **Step 3: Fix any hard errors found**

For each skill that errors, edit its SKILL.md to remove the reserved word, XML tag, or malformed frontmatter. Common fixes:
- Reserved word in name: rename (e.g. `anthropic-foo` → `vendor-foo`).
- XML tag in description: replace with markdown or remove.
- Malformed frontmatter: fix YAML syntax.

After each fix, re-run the test for that single skill:

```bash
pytest tests/skills_hub/test_bundled_corpus_compliance.py -k "<skill_name>" -v
```

- [ ] **Step 4: Run the warning-summary test for visibility**

```bash
pytest tests/skills_hub/test_bundled_corpus_compliance.py::test_bundled_corpus_warning_summary -v -s
```

Capture the warning counts. We expect:
- `body.size_warn`: ~14 (the over-500-line skills)
- `description.voice`: unknown count (depends on how Hermes-style descriptions look)

Save this output to inform the migration report (Task 19).

- [ ] **Step 5: Commit (test + any error fixes)**

```bash
git add tests/skills_hub/test_bundled_corpus_compliance.py opencomputer/skills/
git commit -m "test(skills): bundled corpus compliance test + fix any hard errors"
```

---

## Task 10: Synthesis prompt rewrite

**Files:**
- Modify: `opencomputer/evolution/prompts/synthesis_request.j2`
- Test: `tests/test_evolution_synthesis_prompt.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_evolution_synthesis_prompt.py`:

```python
"""Verify the synthesis prompt teaches Anthropic-spec-compliant voice and naming."""
from pathlib import Path

import jinja2


PROMPT_PATH = (
    Path(__file__).parent.parent
    / "opencomputer" / "evolution" / "prompts" / "synthesis_request.j2"
)


def _render() -> str:
    text = PROMPT_PATH.read_text()
    template = jinja2.Template(text)
    return template.render(
        proposal=type("Proposal", (), {
            "pattern_summary": "user repeatedly runs grep then opens matches in editor",
            "pattern_key": "bash:grep:success",
            "sample_arguments": ["grep -r 'foo' .", "grep -i 'bar' src/"],
        })(),
        existing_names=["read-then-edit", "grep-then-read"],
        max_chars=8000,
    )


def test_prompt_teaches_third_person_voice():
    text = _render()
    assert "third-person" in text.lower() or "3rd-person" in text.lower()
    assert "Processes" in text or "Synthesizes" in text or "Generates" in text


def test_prompt_forbids_first_and_second_person():
    text = _render()
    # Must explicitly tell the LLM not to use 1st/2nd person.
    forbidden_markers = ["never start with", "i", "you", "let me"]
    text_lower = text.lower()
    assert all(m in text_lower for m in forbidden_markers), \
        f"prompt missing forbidden-person markers: {forbidden_markers}"


def test_prompt_requires_what_and_when():
    text = _render()
    text_lower = text.lower()
    assert "what" in text_lower and "when" in text_lower
    assert "use when" in text_lower


def test_prompt_recommends_gerund_naming():
    text = _render()
    text_lower = text.lower()
    assert "gerund" in text_lower
    # Example pair must be in the prompt for the LLM to learn from.
    assert "processing-pdfs" in text or "analyzing-spreadsheets" in text


def test_prompt_forbids_time_sensitive_content():
    text = _render()
    text_lower = text.lower()
    # The phrase "time-sensitive" or a similar marker must appear.
    assert "time-sensitive" in text_lower or "after august" in text_lower


def test_prompt_description_length_cap_280():
    text = _render()
    assert "280" in text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_evolution_synthesis_prompt.py -v
```

Expected: most tests FAIL — current prompt teaches "Use when..." (2nd person) and has 100-char cap.

- [ ] **Step 3: Rewrite the prompt**

Replace the current `synthesis_request.j2` body (per spec §5.3). Full file:

```jinja2
You observed a repeated pattern in this user's session.

Pattern: {{ proposal.pattern_summary }}
Pattern key: {{ proposal.pattern_key }}
Sample tool arguments:
{%- for s in proposal.sample_arguments %}
  - {{ s }}
{%- endfor %}

Draft a SKILL.md that codifies a *reusable* workflow for this pattern.

Hard rules:

1. **Frontmatter** (YAML, between two `---` lines):
   - `name`: lowercase + hyphens only, ≤50 chars. **Use gerund form** —
     `processing-pdfs` not `pdf-helper`; `analyzing-spreadsheets` not
     `excel-utility`. Must NOT duplicate any of these existing names:
     {{ existing_names | join(", ") }}.
     Must NOT contain reserved words: anthropic, claude.
   - `description`: ONE line, ≤280 characters. **Third-person voice** —
     describe what the skill does, like a system describing itself:
     "Processes...", "Synthesizes...", "Generates...".
     **NEVER** start with "I", "You", "We", "Let me", "I can help".
     Must include BOTH:
       - WHAT the skill does (the action verb phrase)
       - WHEN to use it (a "Use when..." clause)
     Pattern: "<3rd-person verb phrase>. Use when <trigger condition>."
     Example: "Synthesizes git commit messages from staged diffs.
              Use when the user asks for help writing commit messages."

2. **Body** (markdown after the closing `---`):
   - `# Title` heading (Title Case, matches the slug).
   - `## When to use` — 2-4 bullets, situation-shaped.
   - `## Steps` — numbered list, imperative voice. ≤ 8 steps.
   - `## Notes` — gotchas / caveats. Optional.

3. **Total length** must be **under {{ max_chars }} characters**, INCLUDING
   the frontmatter. Concise + opinionated > verbose + hedged.

4. **Must NOT include**:
   - Any specific user data, file paths, or session content from the
     samples above.
   - Shell commands that delete data (`rm -rf`, `format`, `eject`, etc.).
   - Personally identifying info from the samples (names, emails,
     IPs, secrets).
   - **Time-sensitive content** like "after August 2025" or "before next
     quarter". If you must reference a deprecated approach, use a
     collapsible "Old patterns" section instead.

5. Output ONLY the SKILL.md content, nothing else (no preamble, no
   ```markdown fences, no explanation). The very first character of
   your output must be `-`.
```

- [ ] **Step 4: Run prompt tests to verify they pass**

```bash
pytest tests/test_evolution_synthesis_prompt.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/evolution/prompts/synthesis_request.j2 tests/test_evolution_synthesis_prompt.py
git commit -m "feat(evolution): rewrite synthesis prompt for 3rd-person + WHAT+WHEN voice"
```

---

## Task 11: Constraints sync + post-synthesis validator hook

**Files:**
- Modify: `opencomputer/evolution/constraints.py`
- Modify: `opencomputer/evolution/synthesize.py`
- Test: `tests/test_evolution_synthesize_skill.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_evolution_synthesize_skill.py`:

```python
def test_constraint_max_description_len_synced_to_280():
    from opencomputer.evolution.constraints import MAX_DESCRIPTION_LEN
    assert MAX_DESCRIPTION_LEN == 280


def test_synthesized_skill_must_pass_strict_validator(tmp_path):
    """A synthesized SKILL.md must pass the unified validator in strict mode."""
    from opencomputer.evolution.synthesize import synthesize_skill_to_dir
    from opencomputer.skills_hub.agentskills_validator import validate_skill_dir

    # Synthetic SKILL.md content (compliant)
    compliant = """---
name: testing-things
description: Tests synthesized skill validation. Use when verifying the synth pipeline.
version: 0.1.0
---

# Testing Things

## When to use
- For tests.

## Steps
1. Test.
"""
    skill_dir = synthesize_skill_to_dir(
        slug="testing-things",
        content=compliant,
        target_dir=tmp_path,
    )
    report = validate_skill_dir(skill_dir, strict=True)
    assert report.passes_strict, f"unexpected issues: {report.errors + report.warnings}"


def test_synthesized_skill_with_reserved_word_rejected(tmp_path):
    from opencomputer.evolution.synthesize import synthesize_skill_to_dir
    from opencomputer.evolution.constraints import ConstraintViolation

    bad = """---
name: anthropic-thing
description: Does anthropic things. Use when needed for anthropic operations.
version: 0.1.0
---

# Anthropic Thing

## When to use
- Always.

## Steps
1. Do it.
"""
    with pytest.raises(ConstraintViolation):
        synthesize_skill_to_dir(
            slug="anthropic-thing",
            content=bad,
            target_dir=tmp_path,
        )
```

(Adjust function names/signatures to match the real `synthesize.py` — read it first.)

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_evolution_synthesize_skill.py -k "max_description_len_synced or strict_validator or reserved_word_rejected" -v
```

Expected: FAIL — `MAX_DESCRIPTION_LEN` is still 500; synthesize doesn't run strict validator.

- [ ] **Step 3: Update constraints.py**

Edit `opencomputer/evolution/constraints.py`:

```python
# Description length cap. Intentionally stricter than Anthropic's 1024-char
# spec ceiling because OpenComputer routes skills by description similarity;
# long descriptions degrade routing precision. 280 chars is enough for the
# WHAT (action verb phrase) + WHEN ("Use when..." clause) the synthesis
# prompt requires.
MAX_DESCRIPTION_LEN = 280
```

- [ ] **Step 4: Add validator hook to synthesize.py**

Find the function that writes the synthesized SKILL.md (likely `synthesize_skill_to_dir` or similar). Before the atomic write, add:

```python
from opencomputer.skills_hub.agentskills_validator import validate_skill_md

def _validate_pre_write(content: str) -> None:
    """Run the unified validator. Errors raise; warnings are logged."""
    report = validate_skill_md(content, strict=True)
    if report.errors:
        msgs = "; ".join(f"{i.rule}: {i.message}" for i in report.errors)
        raise ConstraintViolation(f"synthesized skill failed validation: {msgs}")
    for warning in report.warnings:
        logger.warning(
            "synthesized skill %s emitted warning: %s",
            slug, warning.rule,
        )
```

Call it from `synthesize_skill_to_dir` immediately before writing.

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_evolution_synthesize_skill.py -v
```

Expected: PASS, including the legacy slug + atomic-write tests.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/evolution/constraints.py opencomputer/evolution/synthesize.py tests/test_evolution_synthesize_skill.py
git commit -m "feat(evolution): sync MAX_DESCRIPTION_LEN to 280 + post-synth strict validator"
```

---

## Task 12: Audit + split research-paper-writing (2,375 lines → multi-file)

**Files:**
- Modify: `opencomputer/skills/research-paper-writing/SKILL.md`
- Create: `opencomputer/skills/research-paper-writing/references/*.md`

- [ ] **Step 1: Inspect the file's section structure**

```bash
grep -n "^## " opencomputer/skills/research-paper-writing/SKILL.md | head -30
```

Capture the top-level h2 sections. Each becomes a candidate reference file.

- [ ] **Step 2: Identify natural split points**

Read `opencomputer/skills/research-paper-writing/SKILL.md` from start to end. Group h2 sections into logical chunks (e.g. "literature-review", "writing-abstract", "structuring-results", "references-and-citations"). Aim to keep SKILL.md under 500 lines after the split.

Document the split plan in a comment or notes file before editing.

- [ ] **Step 3: Create reference files (one per logical chunk)**

For each chunk, create `opencomputer/skills/research-paper-writing/references/<chunk-slug>.md` containing:

```markdown
# <Chunk Title>

## Contents
- Section A
- Section B
- Section C

[Original h2 sections moved here, with their h3+ subsections intact]
```

Each reference file >100 lines must have a TOC at the top (per Anthropic spec).

- [ ] **Step 4: Update SKILL.md to point at reference files**

In `SKILL.md`, replace the moved sections with brief pointers:

```markdown
## Literature review

For the full literature-review workflow, see [references/literature-review.md](references/literature-review.md).

Quick start: ...
```

Verify: SKILL.md is now ≤500 lines.

```bash
wc -l opencomputer/skills/research-paper-writing/SKILL.md
```

- [ ] **Step 5: Update internal cross-references**

Search for any anchor links that pointed to moved sections:

```bash
grep -n '#' opencomputer/skills/research-paper-writing/SKILL.md | grep -v '^#'
```

Fix any broken anchors.

- [ ] **Step 6: Run validator on the split skill**

```bash
pytest tests/skills_hub/test_bundled_corpus_compliance.py -k "research-paper-writing" -v
```

Expected: PASS, no body.size_warn warning.

- [ ] **Step 7: Commit**

```bash
git add opencomputer/skills/research-paper-writing/
git commit -m "refactor(skills): split research-paper-writing (2375 → 500 lines via 4-5 reference files)"
```

---

## Task 13: Audit + split claude-code (744 lines → multi-file)

Same pattern as Task 12, applied to `opencomputer/skills/claude-code/SKILL.md`.

- [ ] **Step 1: Inspect h2 structure**

```bash
grep -n "^## " opencomputer/skills/claude-code/SKILL.md
```

- [ ] **Step 2: Plan the split** (~2-3 reference files for 744 lines)

- [ ] **Step 3: Create reference files**

- [ ] **Step 4: Update SKILL.md to point at refs; verify ≤500 lines**

- [ ] **Step 5: Verify validator passes**

```bash
pytest tests/skills_hub/test_bundled_corpus_compliance.py -k "claude-code" -v
```

- [ ] **Step 6: Commit**

```bash
git add opencomputer/skills/claude-code/
git commit -m "refactor(skills): split claude-code skill (744 → ≤500 lines)"
```

---

## Task 14: Audit + split hermes-agent (705 lines → multi-file)

Same pattern as Task 12 + 13, applied to `opencomputer/skills/hermes-agent/SKILL.md`.

- [ ] **Step 1: Inspect h2 structure**
- [ ] **Step 2: Plan the split**
- [ ] **Step 3: Create reference files**
- [ ] **Step 4: Update SKILL.md; verify ≤500 lines**
- [ ] **Step 5: Verify validator passes**

```bash
pytest tests/skills_hub/test_bundled_corpus_compliance.py -k "hermes-agent" -v
```

- [ ] **Step 6: Commit**

```bash
git add opencomputer/skills/hermes-agent/
git commit -m "refactor(skills): split hermes-agent skill (705 → ≤500 lines)"
```

---

## Task 15: Add size_review_date to the 11 exempted skills

**Files:**
- Modify: 11 SKILL.md files in `opencomputer/skills/` (the >500-line skills not split in Tasks 12-14)

The 11 exempt skills (per spec §5.5):
1. `writing-skills` (655)
2. `outlines` (655)
3. `weights-and-biases` (593)
4. `dspy` (593)
5. `audiocraft` (567)
6. `p5js` (547)
7. `coding-standards` (523)
8. `github-repo-management` (515)
9. `llm-wiki` (506)
10. `prp-plan` (505)
11. `segment-anything` (503)

- [ ] **Step 1: For each skill, add `size_review_date: 2026-05-02` to its frontmatter**

Example for `opencomputer/skills/writing-skills/SKILL.md`:

```yaml
---
name: writing-skills
description: ...
version: 0.x.0
size_review_date: 2026-05-02
---
```

If a skill's frontmatter is missing the closing `---`, fix that too.

Do this for all 11 files.

- [ ] **Step 2: Run validator on the exempted skills**

```bash
pytest tests/skills_hub/test_bundled_corpus_compliance.py -v 2>&1 | grep -E "(PASSED|FAILED).*(writing-skills|outlines|weights-and-biases|dspy|audiocraft|p5js|coding-standards|github-repo-management|llm-wiki|prp-plan|segment-anything)"
```

Expected: all PASS, no body.size_warn warnings.

- [ ] **Step 3: Run the warning summary again to confirm size_warn dropped**

```bash
pytest tests/skills_hub/test_bundled_corpus_compliance.py::test_bundled_corpus_warning_summary -v -s
```

Expected: `body.size_warn` count is 0.

- [ ] **Step 4: Commit**

```bash
git add opencomputer/skills/
git commit -m "chore(skills): add size_review_date exemption to 11 large domain-API skills"
```

---

## Task 16: Write docs/skills/AUTHORING.md

**Files:**
- Create: `docs/skills/AUTHORING.md`

- [ ] **Step 1: Write the authoring guide**

```bash
mkdir -p docs/skills
```

Create `docs/skills/AUTHORING.md`:

```markdown
# Skill Authoring Guide

OpenComputer skills follow the [Anthropic Agent Skills spec](https://docs.claude.com/en/agents-and-tools/agent-skills/best-practices) with two intentional divergences (documented inline below).

## Frontmatter rules

### `name`
- Lowercase letters + digits + hyphens only.
- ≤50 characters.
- **Gerund form preferred:** `processing-pdfs`, not `pdf-helper`. `analyzing-spreadsheets`, not `excel-utility`.
- **Forbidden:** any token equal to `anthropic` or `claude` (case-insensitive).

### `description`
- ≤280 characters. (OpenComputer caps this stricter than Anthropic's 1024 because routing degrades on long descriptions.)
- **Third-person voice.** Describe what the skill does, like a system describing itself.
  - ✅ `Processes PDF files and extracts text. Use when working with PDFs.`
  - ❌ `I can help you extract text from PDFs.`
  - ❌ `You can use this to extract text.`
- **Must include both WHAT and WHEN.**
  - Pattern: `<3rd-person verb phrase>. Use when <trigger condition>.`
- No XML/HTML tags.

### Optional fields
- `version`: semver string (e.g. `0.1.0`).
- `size_review_date`: ISO date (e.g. `2026-05-02`). Documents an intentional exemption from the body-size warning. Use when a skill genuinely earns its >500-line size.

## Body rules

- Top-level `# Title` heading matches the slug in Title Case.
- ≤500 lines for optimal performance. Split larger skills into reference files.
- Forward slashes only in paths (`reference/foo.md`, not `reference\foo.md`).
- No time-sensitive content (`after August 2025`, `before next quarter`). Use a collapsible "Old patterns" section if you must reference deprecated approaches.

## Reference files

- Place under `<skill>/references/` or `<skill>/examples/`.
- ≤1 level deep from SKILL.md (no SKILL → A → B chains; Anthropic-skill loaders do partial reads on nested files and miss content).
- Files >100 lines must have a TOC at the top.

## Worked examples

### Good

```yaml
---
name: writing-commit-messages
description: Synthesizes conventional-commit messages from staged diffs. Use when the user asks for help writing or improving a git commit message.
version: 0.2.0
---

# Writing Commit Messages

## When to use
- The user asks for a commit message.
- The user has staged changes and asks for help.
- The user wants to improve an existing commit message.

## Steps
1. Run `git diff --staged` to read the changes.
2. Identify the type (feat, fix, refactor, docs, chore, test).
3. Identify the scope (subsystem touched).
4. Write a one-line subject ≤72 chars.
5. If non-trivial, add a 2-3 sentence body.
```

### Bad (rewritten to good)

❌ Original:
```yaml
---
name: pdf-helper
description: I can help you with PDFs! Just tell me what you want to do.
---
```

✅ Fixed:
```yaml
---
name: processing-pdfs
description: Processes PDF files — extracts text, fills forms, merges documents. Use when working with PDF files or when the user mentions PDFs, forms, or document extraction.
version: 0.1.0
---
```

What changed:
- `pdf-helper` (noun) → `processing-pdfs` (gerund).
- 1st-person ("I can help you") → 3rd-person ("Processes").
- Vague ("Just tell me what you want") → specific WHAT + WHEN.

## Validation

Before committing a skill, run:

```bash
pytest tests/skills_hub/test_bundled_corpus_compliance.py -k "<your-skill-slug>" -v
```

This runs the unified validator in lenient mode. Hard errors (reserved words, XML, malformed frontmatter) block the commit; warnings are advisory.

For new skills (not yet in the bundled corpus), use strict mode:

```python
from opencomputer.skills_hub.agentskills_validator import validate_skill_dir
report = validate_skill_dir(Path("path/to/your-skill"))
report.raise_if_errors()
```
```

- [ ] **Step 2: Commit**

```bash
git add docs/skills/AUTHORING.md
git commit -m "docs(skills): add AUTHORING.md authoring guide with worked examples"
```

---

## Task 17: Update docs/evolution/README.md description style guide

**Files:**
- Modify: `docs/evolution/README.md`

- [ ] **Step 1: Read the current README to find the right insertion point**

```bash
grep -n "^## " docs/evolution/README.md
```

Find a sensible section (e.g. after the "How synthesis works" section).

- [ ] **Step 2: Insert the description style guide section**

Add after the synthesis-explanation section:

```markdown
## Description style guide

Synthesized skill descriptions must be:

- **Third-person.** "Processes...", "Synthesizes...", "Generates...".  Never "I can help" or "You can use".
- **WHAT + WHEN.** The action verb phrase, then a "Use when..." clause.

### Examples

✅ Good:
- `Synthesizes git commit messages from staged diffs. Use when the user asks for help writing commit messages.`
- `Detects repeated grep-then-edit patterns in a session. Use when investigating workflow patterns.`

❌ Bad:
- `I can help you write commit messages.`
- `Use when you want to write commits.` (no WHAT)
- `Helps with git stuff.` (no WHEN, vague WHAT)

The synthesis prompt enforces this voice. The post-synthesis validator catches non-compliant descriptions before they're written. See [docs/skills/AUTHORING.md](../skills/AUTHORING.md) for the full spec.
```

- [ ] **Step 3: Commit**

```bash
git add docs/evolution/README.md
git commit -m "docs(evolution): add description style guide section"
```

---

## Task 18: Write migration report

**Files:**
- Create: `docs/skills/2026-05-02-bundled-corpus-audit.md`

- [ ] **Step 1: Run validator one more time to capture final state**

```bash
pytest tests/skills_hub/test_bundled_corpus_compliance.py::test_bundled_corpus_warning_summary -v -s 2>&1 | tee /tmp/corpus-audit.txt
```

- [ ] **Step 2: Write the migration report**

Create `docs/skills/2026-05-02-bundled-corpus-audit.md`:

```markdown
# Bundled Corpus Compliance Audit — 2026-05-02

Initial state: 127 bundled skills under `opencomputer/skills/`. Audit triggered by adoption of Anthropic's published Agent Skills spec.

## Hard errors found and fixed

[List any reserved-word, XML-tag, or frontmatter-parse errors fixed during Task 9, with skill name and what was wrong. If none, state "No hard errors found."]

## Body-size violations (14 skills > 500 lines)

### Split into reference files (3 skills)

| Skill | Original lines | Final SKILL.md lines | Reference files added |
|---|---|---|---|
| research-paper-writing | 2,375 | <fill in> | <list> |
| claude-code | 744 | <fill in> | <list> |
| hermes-agent | 705 | <fill in> | <list> |

### Exempted via `size_review_date` (11 skills)

These skills are domain-API references where size reflects API-surface density. Splitting would harm discoverability.

| Skill | Lines | Rationale |
|---|---|---|
| writing-skills | 655 | Meta-skill about skill authoring; cross-references would multiply confusion. |
| outlines | 655 | Single-domain library reference. |
| weights-and-biases | 593 | Single-API reference; sections are interdependent. |
| dspy | 593 | Single-API reference. |
| audiocraft | 567 | Single-library reference. |
| p5js | 547 | Single-library reference. |
| coding-standards | 523 | Single-domain reference; sections are interdependent. |
| github-repo-management | 515 | Single-domain reference. |
| llm-wiki | 506 | Curated knowledge base. |
| prp-plan | 505 | Single-workflow reference. |
| segment-anything | 503 | Single-library reference. |

Re-evaluate at next year's audit (2027-05-02 or earlier if a skill grows past ~750 lines).

## Voice-violation warnings (count: <fill in from Step 1>)

Cleanup deferred to a follow-up PR. Each violation:
- Logged in the validator output for tracking.
- Does not block ingestion (warning, not error).
- Will be cleaned up in batches per skill domain.

## Outcome

- ✅ All 127 bundled skills pass `validate_skill_md(strict=False)` with zero errors.
- ✅ 0 `body.size_warn` warnings remaining (all addressed via split or exemption).
- ⚠️ <N> voice-violation warnings — tracked, deferred.
```

(Fill in the bracketed counts after running the validator.)

- [ ] **Step 3: Commit**

```bash
git add docs/skills/2026-05-02-bundled-corpus-audit.md
git commit -m "docs(skills): bundled corpus audit migration report (2026-05-02)"
```

---

## Task 19: Final verification

- [ ] **Step 1: Run the full pytest suite**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
pytest tests/ --tb=short 2>&1 | tail -20
```

Expected: all tests pass. Compare count to baseline from Step 0b — count should be at least baseline + new tests added.

- [ ] **Step 2: Run ruff**

```bash
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: clean (or only pre-existing warnings from baseline).

- [ ] **Step 3: Run the bundled corpus test in verbose mode**

```bash
pytest tests/skills_hub/test_bundled_corpus_compliance.py -v 2>&1 | tail -30
```

Expected: all 127+ skill cases PASS. Warning summary shows 0 `body.size_warn` and a finite (logged) count of `description.voice` warnings.

- [ ] **Step 4: Verify success criteria from spec §9**

Check each box from the spec:
- [ ] `pytest tests/skills_hub/test_bundled_corpus_compliance.py` passes — zero errors.
- [ ] All 4 new checks have passing unit tests.
- [ ] Synthesis prompt updated; new test verifies generated skills pass strict validator.
- [ ] `MAX_DESCRIPTION_LEN` synchronized between prompt cap and constraint constant.
- [ ] `skill_manage.py` delegates to hub validator; old `_validate_frontmatter` path removed.
- [ ] 3 large bundled skills split; 11 exempted; migration report committed.
- [ ] `docs/skills/AUTHORING.md` exists with worked examples.
- [ ] `docs/evolution/README.md` updated with description style guide.
- [ ] Full pytest suite green; ruff clean.

- [ ] **Step 5: Push the branch**

```bash
git push -u origin feat/skill-spec-compliance
```

- [ ] **Step 6: Open the PR**

```bash
gh pr create --title "feat(skills): Anthropic spec compliance — unify validators, fix synth prompt, audit corpus" --body "$(cat <<'EOF'
## Summary

SP1 of the Anthropic-API-parity scope. Spec: `docs/superpowers/specs/2026-05-02-skill-spec-compliance-design.md`.

- Unify two validators (`skill_manage` + `agentskills_validator`) into a single source of truth with a `ValidationReport` API.
- Add 4 missing frontmatter checks: reserved words (`anthropic`, `claude`), XML tags in name/description, 3rd-person voice (warning), body >500 lines (warning).
- Rewrite auto-skill-evolution synthesis prompt to teach 3rd-person + WHAT+WHEN voice + gerund naming.
- Sync `MAX_DESCRIPTION_LEN` to 280 (intentionally stricter than Anthropic's 1024 — routing degrades on long descriptions).
- Bundled corpus audit: 3 large skills (`research-paper-writing`, `claude-code`, `hermes-agent`) split into reference files; 11 domain-API skills exempted with `size_review_date` frontmatter.
- New: `tests/skills_hub/test_bundled_corpus_compliance.py` regression suite over the entire bundled corpus.
- New: `docs/skills/AUTHORING.md` authoring guide; `docs/skills/2026-05-02-bundled-corpus-audit.md` migration report.

## Test plan

- [ ] `pytest tests/` — full suite green
- [ ] `ruff check` — clean
- [ ] `pytest tests/skills_hub/test_bundled_corpus_compliance.py -v -s` — zero errors, voice-warning count documented in report
- [ ] Manual: synthesize a skill via `oc evolution reflect` (or equivalent), verify the description is 3rd-person + has WHAT+WHEN

## Out of scope

- SP2 (PDF + Provider Hardening — including Bedrock citations footgun): separate sub-project.
- SP3 (Files API + artifact loop): separate sub-project.
- SP4 (Server-side tools / Skills-via-API): demand-gated, separate decision.
- Voice-violation cleanup of the 127 bundled skills: tracked, deferred to follow-up PR.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Checklist (run after writing the plan)

**Spec coverage:** Walk each section of the spec and point to the task implementing it.

| Spec section | Task |
|---|---|
| §5.1 Validator unification | Tasks 1, 2, 7, 8 |
| §5.2 4 missing checks | Tasks 3, 4, 5, 6 |
| §5.3 Synthesis prompt rewrite | Task 10 |
| §5.4 Constraint synchronization | Task 11 |
| §5.5 Bundled corpus migration | Tasks 9, 12, 13, 14, 15 |
| §5.6 Test plan | Tasks 1-9, 11 (each adds tests inline) |
| §5.7 Documentation | Tasks 16, 17, 18 |
| §9 Success criteria verification | Task 19 |

**Placeholder scan:** No "TBD", "TODO", "fill in details", "implement later" outside the migration-report template (where bracketed `<fill in>` placeholders are intentional — the values are observed at runtime, not pre-known).

**Type consistency:**
- `ValidationReport` and `ValidationIssue` have consistent field names across all tasks.
- `validate_skill_md(text, *, strict=True, path=None)` signature is identical in Tasks 2 and 7.
- `RESERVED_WORDS` constant defined in Task 3, referenced consistently.
