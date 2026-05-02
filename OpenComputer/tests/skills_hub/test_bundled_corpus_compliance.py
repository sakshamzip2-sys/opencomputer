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
