"""GitHubSource — clones an arbitrary public GitHub repo and walks for SKILL.md.

Identifier shape: ``<user>/<repo>/<skill-name>``. Source name = ``<user>/<repo>``.

Uses subprocess git clone with depth=1 — no gitpython dep. Caller can refresh
by deleting ``_clone_dir`` and calling ``_walk_skills`` again. MVP does not
support authenticated clones; ``GITHUB_TOKEN`` integration is a follow-up.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from opencomputer.skills_hub.agentskills_validator import validate_frontmatter
from plugin_sdk.skill_source import SkillBundle, SkillMeta, SkillSource

_log = logging.getLogger(__name__)


class GitHubSource(SkillSource):
    def __init__(self, repo: str, clone_root: Path) -> None:
        """`repo` is "user/name". `clone_root` is where clones live."""
        self._repo = repo
        if "/" not in repo:
            raise ValueError(f"repo must be 'user/name', got {repo!r}")
        self._user, self._name = repo.split("/", 1)
        self._clone_root = Path(clone_root)
        self._clone_dir = self._clone_root / self._user / self._name

    @property
    def name(self) -> str:
        return self._repo

    def _ensure_cloned(self) -> None:
        if self._clone_dir.exists():
            return
        self._clone_dir.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{self._repo}.git"
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", url, str(self._clone_dir)],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as e:
            _log.warning("git clone failed for %s: %s", self._repo, e.stderr)
            raise
        except subprocess.TimeoutExpired:
            _log.warning("git clone timed out for %s", self._repo)
            raise

    def _walk_skills(self) -> list[Path]:
        if not self._clone_dir.exists():
            try:
                self._ensure_cloned()
            except Exception:
                return []
        return list(self._clone_dir.rglob("SKILL.md"))

    def _meta_from_skill_md(self, skill_md_path: Path) -> SkillMeta | None:
        try:
            text = skill_md_path.read_text()
            parsed = validate_frontmatter(text)
        except Exception as e:
            _log.debug("skipping %s: %s", skill_md_path, e)
            return None
        name = parsed["name"]
        return SkillMeta(
            identifier=f"{self._repo}/{name}",
            name=name,
            description=parsed["description"],
            source=self.name,
            version=parsed.get("version"),
            author=parsed.get("author"),
            tags=tuple(parsed.get("tags", [])),
            trust_level="community",
        )

    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        out: list[SkillMeta] = []
        q = query.lower()
        for skill_md in self._walk_skills():
            meta = self._meta_from_skill_md(skill_md)
            if meta is None:
                continue
            if q == "" or q in meta.name.lower() or q in meta.description.lower():
                out.append(meta)
            if len(out) >= limit:
                break
        return out

    def inspect(self, identifier: str) -> SkillMeta | None:
        for skill_md in self._walk_skills():
            meta = self._meta_from_skill_md(skill_md)
            if meta and meta.identifier == identifier:
                return meta
        return None

    def fetch(self, identifier: str) -> SkillBundle | None:
        for skill_md in self._walk_skills():
            meta = self._meta_from_skill_md(skill_md)
            if meta and meta.identifier == identifier:
                return SkillBundle(
                    identifier=identifier,
                    skill_md=skill_md.read_text(),
                    files={},
                )
        return None
