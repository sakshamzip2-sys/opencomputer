"""Description token-overlap collision detector (Phase 5.3).

The activation matcher fires when a user prompt overlaps a skill's
description by ≥2 tokens. If a new skill's description overlaps an
existing skill's by >50%, both will fire on the same prompts — that's
a sign we should have *extended* the existing skill, not added a new
one.

This module is the gate. The CLI calls it on every approve action.
"""

from __future__ import annotations

import re
from pathlib import Path

import frontmatter


def _tokenize(s: str) -> set[str]:
    """Match the registry's tokenizer: lowercase, words ≥3 chars."""
    return {w for w in re.findall(r"\w+", (s or "").lower()) if len(w) >= 3}


def _read_description(skill_md: Path) -> str:
    try:
        post = frontmatter.load(skill_md)
        return str(post.get("description") or "")
    except Exception:  # noqa: BLE001 — corrupt frontmatter is an empty-overlap signal
        return ""


def find_collision(
    draft_path: Path,
    *,
    bundled_skills_dir: Path,
    profile_skills_dir: Path,
    threshold: float = 0.5,
) -> str | None:
    """Return the colliding skill name (slug), or ``None``.

    Walks bundled + profile-local approved skills. For each existing
    skill, computes ``|draft ∩ existing| / |existing|`` over description
    tokens. If any existing skill is ≥``threshold`` covered by the
    draft's tokens, returns that skill's slug.

    Why divide by ``|existing|`` not ``|draft|``: a draft adding a new
    distinctive token shouldn't count against itself. We're asking
    "does the existing skill already do this?" — that's
    ``|overlap|/|existing|``.
    """
    draft = frontmatter.load(draft_path)
    draft_tokens = _tokenize(draft.get("description") or "")
    if not draft_tokens:
        return None

    for skills_dir in (bundled_skills_dir, profile_skills_dir):
        if not skills_dir or not skills_dir.is_dir():
            continue
        for skill_md in skills_dir.glob("*/SKILL.md"):
            existing_tokens = _tokenize(_read_description(skill_md))
            if not existing_tokens:
                continue
            overlap = len(draft_tokens & existing_tokens) / len(existing_tokens)
            if overlap >= threshold:
                return skill_md.parent.name
    return None
