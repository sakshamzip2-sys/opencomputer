"""
Three-pillar memory manager.

- Declarative: MEMORY.md + USER.md (plain markdown the user/agent edit)
- Procedural:  ~/.opencomputer/skills/*/SKILL.md (skills folder)
- Episodic:    SQLite + FTS5 (via SessionDB, not here)

This module owns the declarative + procedural reads/writes.
Episodic memory is queried through SessionDB in state.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import frontmatter


@dataclass(frozen=True, slots=True)
class SkillMeta:
    """Lightweight skill metadata — from frontmatter, without loading the body."""

    id: str
    name: str
    description: str
    path: Path
    version: str = "0.1.0"


class MemoryManager:
    """Reads declarative memory and lists procedural (skill) memory.

    Skills are searched across multiple roots (kimi-cli pattern):
      1. User skills: ~/.opencomputer/skills/   (write target for new skills)
      2. Bundled skills: <repo>/opencomputer/skills/ (read-only, shipped defaults)

    Higher-priority roots shadow lower-priority ones by skill id.
    """

    def __init__(
        self,
        declarative_path: Path,
        skills_path: Path,
        bundled_skills_paths: list[Path] | None = None,
    ) -> None:
        self.declarative_path = declarative_path
        self.skills_path = skills_path
        self.skills_path.mkdir(parents=True, exist_ok=True)
        # Always include bundled skills shipped with core at the lowest priority
        if bundled_skills_paths is None:
            bundled = Path(__file__).resolve().parent.parent / "skills"
            bundled_skills_paths = [bundled] if bundled.exists() else []
        self.bundled_skills_paths = bundled_skills_paths

    # ─── declarative ──────────────────────────────────────────────

    def read_declarative(self) -> str:
        """Return the entire MEMORY.md contents (empty string if missing)."""
        if not self.declarative_path.exists():
            return ""
        return self.declarative_path.read_text(encoding="utf-8")

    def append_declarative(self, text: str) -> None:
        """Append a block of text to MEMORY.md."""
        self.declarative_path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.read_declarative()
        separator = "\n\n" if existing and not existing.endswith("\n\n") else ""
        self.declarative_path.write_text(
            existing + separator + text.strip() + "\n",
            encoding="utf-8",
        )

    # ─── procedural (skills) ─────────────────────────────────────

    def list_skills(self) -> list[SkillMeta]:
        """Scan all skill roots for SKILL.md files. User skills shadow bundled ones."""
        roots = [self.skills_path, *self.bundled_skills_paths]
        seen_ids: set[str] = set()
        out: list[SkillMeta] = []
        for root in roots:
            if not root.exists():
                continue
            for skill_dir in root.iterdir():
                if not skill_dir.is_dir() or skill_dir.name in seen_ids:
                    continue
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.exists():
                    continue
                try:
                    post = frontmatter.load(skill_md)
                except Exception:
                    continue
                meta = post.metadata
                seen_ids.add(skill_dir.name)
                out.append(
                    SkillMeta(
                        id=skill_dir.name,
                        name=str(meta.get("name", skill_dir.name)),
                        description=str(meta.get("description", "")),
                        path=skill_md,
                        version=str(meta.get("version", "0.1.0")),
                    )
                )
        return out

    def load_skill_body(self, skill_id: str) -> str:
        """Load the full text of a skill's SKILL.md (minus frontmatter)."""
        skill_md = self.skills_path / skill_id / "SKILL.md"
        if not skill_md.exists():
            return ""
        post = frontmatter.load(skill_md)
        return post.content

    def write_skill(
        self, skill_id: str, description: str, body: str, version: str = "0.1.0"
    ) -> Path:
        """Create (or overwrite) a skill at ~/.opencomputer/skills/<skill_id>/SKILL.md."""
        skill_dir = self.skills_path / skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"
        post = frontmatter.Post(
            body,
            name=skill_id,
            description=description,
            version=version,
        )
        skill_md.write_text(frontmatter.dumps(post), encoding="utf-8")
        return skill_md


__all__ = ["MemoryManager", "SkillMeta"]
