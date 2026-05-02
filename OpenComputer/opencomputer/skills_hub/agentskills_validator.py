"""Validate SKILL.md frontmatter against the agentskills.io standard.

Standard (best-effort inferred from Hermes Agent docs and the published
inventory; tighten when an official spec is canonically published):

- Required: ``name``, ``description``
- Optional, recommended: ``version`` (semver), ``author``, ``tags`` (list of strings)
- ``name`` is kebab-case
- ``description`` is 20-500 chars

Reference: https://agentskills.io
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")

DESCRIPTION_MIN = 20
DESCRIPTION_MAX = 500


class ValidationError(ValueError):
    """Raised when SKILL.md frontmatter does not satisfy agentskills.io."""


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


def validate_frontmatter(skill_md: str) -> dict[str, Any]:
    """Parse and validate the YAML frontmatter at the top of SKILL.md.

    Returns the parsed dict. Raises ValidationError on any failure.
    """
    if not skill_md.lstrip().startswith("---"):
        raise ValidationError("SKILL.md has no frontmatter (must start with '---')")

    body = skill_md.lstrip()
    rest = body[3:]
    end_idx = rest.find("\n---")
    if end_idx == -1:
        raise ValidationError("SKILL.md has unclosed frontmatter (no closing '---')")

    yaml_block = rest[:end_idx]
    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError as e:
        raise ValidationError(f"frontmatter is not valid YAML: {e}") from e

    if not isinstance(parsed, dict):
        raise ValidationError("frontmatter must be a YAML mapping (key: value pairs)")

    if "name" not in parsed or not parsed["name"]:
        raise ValidationError("missing required field 'name'")
    if "description" not in parsed or not parsed["description"]:
        raise ValidationError("missing required field 'description'")

    name = str(parsed["name"])
    if not _NAME_RE.match(name):
        raise ValidationError(
            f"name {name!r} must be kebab-case (lowercase letters, digits, hyphens; "
            "start with a letter; no leading/trailing/double hyphens)"
        )

    desc = str(parsed["description"])
    if len(desc) < DESCRIPTION_MIN:
        raise ValidationError(
            f"description must be at least {DESCRIPTION_MIN} chars (got {len(desc)})"
        )
    if len(desc) > DESCRIPTION_MAX:
        raise ValidationError(
            f"description must be at most {DESCRIPTION_MAX} chars (got {len(desc)})"
        )

    if "version" in parsed and parsed["version"] is not None:
        version = str(parsed["version"])
        if not _SEMVER_RE.match(version):
            raise ValidationError(
                f"version {version!r} must be semver (e.g. 1.0.0 or 1.0.0-beta.1)"
            )

    if "tags" in parsed and parsed["tags"] is not None:
        tags = parsed["tags"]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValidationError("tags must be a list of strings")

    return parsed


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

    if not isinstance(parsed, dict):
        report.errors.append(ValidationIssue(
            rule="frontmatter.parse_error",
            severity="error",
            field=None,
            message="frontmatter must be a YAML mapping (key: value pairs)",
        ))
        return report

    # Existing checks (name regex, description length, version semver, tags type).
    # Convert raises to issues.
    _run_legacy_checks(text, report)

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
    return yaml.safe_load(text) or {}


def _run_legacy_checks(text: str, report: ValidationReport) -> None:
    """Run the existing validate_frontmatter checks, converting raises to issues.

    The legacy validator takes raw SKILL.md text and raises on the first
    error. We wrap it so failures land in the structured report.
    """
    try:
        validate_frontmatter(text)
    except ValidationError as exc:
        report.errors.append(ValidationIssue(
            rule="legacy",
            severity="error",
            field=None,
            message=str(exc),
        ))


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


def _check_xml_tags(parsed: dict, report: ValidationReport) -> None:
    """Stub — implemented in Task 4."""
    pass


def _check_description_voice(description, report: ValidationReport) -> None:
    """Stub — implemented in Task 5."""
    pass


def _check_body_size(body: str, parsed: dict, report: ValidationReport) -> None:
    """Stub — implemented in Task 6."""
    pass
