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
from typing import Any

import yaml

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")

DESCRIPTION_MIN = 20
DESCRIPTION_MAX = 500


class ValidationError(ValueError):
    """Raised when SKILL.md frontmatter does not satisfy agentskills.io."""


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
