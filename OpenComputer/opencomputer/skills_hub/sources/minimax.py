"""MiniMaxSource — default-tap SkillSource for MiniMax-AI/cli skills (Wave 6.E.2).

Subclasses :class:`GitHubSource` to add a stable ``minimax/<skill-name>``
identifier prefix that's independent of the upstream repo path. Users
can install MiniMax-AI skills via::

    oc skills install minimax/<skill-name>

without having to know the underlying GitHub repo.

The MiniMax CLI skills live at https://github.com/MiniMax-AI/cli — they
are vanilla SKILL.md format compatible with our YAML-front-matter
parser. No translation layer required; we just clone + walk + filter to
the ``skills/`` subdirectory if present (otherwise walk the whole tree).

Why a dedicated class instead of just adding ``MiniMax-AI/cli`` as a
github tap? Two reasons:

1. **Stable identifier prefix.** A user-added github tap produces
   identifiers like ``MiniMax-AI/cli/foo``. If the upstream repo is
   ever moved, every previously-installed identifier breaks. A
   dedicated class freezes the prefix at ``minimax/`` and the class
   internally maps that to whatever the current upstream repo is.

2. **Default-tap status.** :func:`_build_router` in
   :mod:`opencomputer.cli_skills_hub` always wires ``MiniMaxSource``
   alongside ``WellKnownSource`` so users don't have to ``oc skills
   tap add`` it before searching.
"""

from __future__ import annotations

import logging
from pathlib import Path

from opencomputer.skills_hub.sources.github import GitHubSource
from plugin_sdk.skill_source import SkillBundle, SkillMeta

_log = logging.getLogger(__name__)

# Hardcoded upstream — change here if the repo ever moves. Identifiers
# like ``minimax/foo`` are decoupled from this so users don't notice a
# migration.
UPSTREAM_REPO = "MiniMax-AI/cli"

# Many CLI projects keep skills under ``skills/`` (matching the
# OpenComputer + Claude Code conventions). We honour that if present
# and otherwise fall back to a full-tree walk.
SKILLS_SUBDIR = "skills"


class MiniMaxSource(GitHubSource):
    """SkillSource pointed at https://github.com/MiniMax-AI/cli.

    Public name = ``minimax`` (stable). Identifier shape =
    ``minimax/<skill-name>``.
    """

    def __init__(self, clone_root: Path) -> None:
        super().__init__(repo=UPSTREAM_REPO, clone_root=clone_root)

    @property
    def name(self) -> str:
        return "minimax"

    def _walk_skills(self) -> list[Path]:
        # Filter to skills/ subdir if it exists.
        if not self._clone_dir.exists():
            try:
                self._ensure_cloned()
            except Exception:  # noqa: BLE001
                return []
        skills_dir = self._clone_dir / SKILLS_SUBDIR
        root = skills_dir if skills_dir.exists() else self._clone_dir
        return list(root.rglob("SKILL.md"))

    def _meta_from_skill_md(self, skill_md_path: Path) -> SkillMeta | None:
        # Reuse parent's parser, then rewrite identifier to use the
        # stable minimax/ prefix instead of MiniMax-AI/cli/.
        meta = super()._meta_from_skill_md(skill_md_path)
        if meta is None:
            return None
        return SkillMeta(
            identifier=f"{self.name}/{meta.name}",
            name=meta.name,
            description=meta.description,
            source=self.name,
            version=meta.version,
            author=meta.author,
            tags=meta.tags,
            trust_level="community",
        )

    def fetch(self, identifier: str) -> SkillBundle | None:
        # ``identifier`` is ``minimax/<name>``. Translate to the
        # filesystem path under our clone (skills/<name>/SKILL.md or
        # <name>/SKILL.md), then build the bundle from disk.
        if not identifier.startswith(f"{self.name}/"):
            return None
        rel = identifier[len(self.name) + 1:]
        if not self._clone_dir.exists():
            try:
                self._ensure_cloned()
            except Exception:
                return None
        # Try skills/<rel>/SKILL.md first, then bare <rel>/SKILL.md
        candidates = [
            self._clone_dir / SKILLS_SUBDIR / rel,
            self._clone_dir / rel,
        ]
        skill_dir: Path | None = None
        for c in candidates:
            if (c / "SKILL.md").exists():
                skill_dir = c
                break
        if skill_dir is None:
            return None
        skill_md = (skill_dir / "SKILL.md").read_text()
        files: dict[str, str] = {}
        # Walk the skill directory and collect text/* siblings
        # capped at 1 MiB each to prevent a malicious upstream from
        # bloating our local install.
        for path in skill_dir.rglob("*"):
            if path.is_file() and path.name != "SKILL.md":
                rel_inside = path.relative_to(skill_dir).as_posix()
                try:
                    if path.stat().st_size > 1_000_000:
                        _log.warning(
                            "minimax skill %s: skipping large file %s (%d bytes)",
                            identifier, rel_inside, path.stat().st_size,
                        )
                        continue
                    files[rel_inside] = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError) as exc:
                    _log.debug("minimax: skipped %s: %s", rel_inside, exc)
        return SkillBundle(
            identifier=identifier,
            skill_md=skill_md,
            files=files,
        )

    def inspect(self, identifier: str) -> SkillMeta | None:
        if not identifier.startswith(f"{self.name}/"):
            return None
        for skill_md in self._walk_skills():
            meta = self._meta_from_skill_md(skill_md)
            if meta is not None and meta.identifier == identifier:
                return meta
        return None


__all__ = ["MiniMaxSource", "UPSTREAM_REPO", "SKILLS_SUBDIR"]
