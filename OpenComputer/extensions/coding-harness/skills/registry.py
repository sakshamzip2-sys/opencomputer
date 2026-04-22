"""SkillRegistry — scans bundled SKILL.md files and parses their frontmatter.

Each bundled skill directory (skills/<name>/SKILL.md) has YAML frontmatter
with `name`, `description`, `version`. The registry parses these cheaply so
the auto-activation injection provider can match user messages against
descriptions without loading the full skill body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SkillEntry:
    id: str  # directory name
    name: str
    description: str
    version: str
    path: Path  # full path to SKILL.md


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.+?)\s*$", re.MULTILINE)


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return {}
    body = m.group(1)
    out: dict[str, str] = {}
    for kv in _KV_RE.finditer(body):
        out[kv.group(1).strip()] = kv.group(2).strip().strip('"').strip("'")
    return out


def discover(skills_dir: Path) -> list[SkillEntry]:
    """Scan `skills_dir/*/SKILL.md` and return one entry per skill.

    Returns deterministic alphabetical order (important for prompt-cache
    stability — same catalogue across turns produces the same bytes).
    """
    out: list[SkillEntry] = []
    if not skills_dir.exists():
        return out
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _parse_frontmatter(text)
        out.append(
            SkillEntry(
                id=child.name,
                name=meta.get("name", child.name),
                description=meta.get("description", ""),
                version=meta.get("version", "0.0.0"),
                path=skill_md,
            )
        )
    return out


def tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric tokens, length >= 3."""
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 3}


def match_skill(user_message: str, skills: Iterable[SkillEntry]) -> SkillEntry | None:
    """Return the best-matching skill, or None if no skill has a strong match.

    "Strong match" = at least 2 tokens from the user message appear in the
    skill's description. Cheap, deterministic, no ML.
    """
    user_tokens = tokenize(user_message)
    best: tuple[int, SkillEntry] | None = None
    for s in skills:
        desc_tokens = tokenize(s.description)
        overlap = len(user_tokens & desc_tokens)
        if overlap < 2:
            continue
        if best is None or overlap > best[0]:
            best = (overlap, s)
    return best[1] if best else None


__all__ = [
    "SkillEntry",
    "discover",
    "tokenize",
    "match_skill",
]
