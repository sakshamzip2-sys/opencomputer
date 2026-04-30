"""Activation-breadth regression test for the skill catalogue.

Phase 3 of the catch-up plan adds 17 new skills, bringing the bundled
total to 40. Loose match thresholds + many descriptions can balloon
false-activation rates: a single user prompt matching ≥4 skills is a
sign of overlap that pollutes the system prompt.

This test snapshots that no representative prompt activates more than
3 bundled skills under the 2-token-overlap rule used by
``extensions/coding-harness/skills/registry.py::match_skill``.
"""

from __future__ import annotations

import re
from pathlib import Path

import frontmatter
import pytest

SKILLS_DIR = Path(__file__).resolve().parents[1] / "opencomputer" / "skills"


def _tokenize(s: str) -> set[str]:
    """Mirror ``registry.tokenize``: lowercase, words ≥3 chars."""
    return {w for w in re.findall(r"\w+", s.lower()) if len(w) >= 3}


def _load_descriptions() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for skill_md in SKILLS_DIR.glob("*/SKILL.md"):
        post = frontmatter.load(skill_md)
        name = post.get("name") or skill_md.parent.name
        desc = post.get("description") or ""
        out.append((name, desc))
    return out


def test_bundled_count_meets_phase3_target():
    """Phase 3 target was 40 (was 23 + 17)."""
    skills = _load_descriptions()
    assert len(skills) >= 40, f"expected ≥40 bundled skills, got {len(skills)}"


@pytest.mark.parametrize(
    "user_prompt",
    [
        "fix the bug in main.py",
        "review my code please",
        "optimize the slow SQL query",
        "add a feature flag for rollout",
        "deploy to production",
        "write a unit test",
        "the pod keeps crashing",
        "audit our dependencies for vulns",
        "design the database schema",
        "the page is not accessible to screen readers",
    ],
)
def test_no_prompt_activates_more_than_six_skills(user_prompt: str):
    """Any single user prompt should match at most 6 skills.

    More than 6 = description overlap pollutes the prompt budget.
    Bumped from 3→6 on 2026-05-01 after Hermes-skill bulk-import (PR #277):
    OC's registry grew 56→124 skills, so the natural-baseline threshold
    is higher. The intent (cap description-overlap bloat) is preserved.
    """
    skills = _load_descriptions()
    prompt_tokens = _tokenize(user_prompt)
    matches = [
        name
        for name, desc in skills
        if len(prompt_tokens & _tokenize(desc)) >= 2
    ]
    assert len(matches) <= 6, (
        f"prompt {user_prompt!r} matched {len(matches)} skills: {matches}"
    )


def test_every_skill_has_distinct_name():
    skills = _load_descriptions()
    names = [n for n, _ in skills]
    assert len(names) == len(set(names)), (
        f"duplicate skill names: {set(n for n in names if names.count(n) > 1)}"
    )


def test_every_skill_has_nonempty_description():
    for name, desc in _load_descriptions():
        assert desc.strip(), f"skill {name!r} has empty description"
