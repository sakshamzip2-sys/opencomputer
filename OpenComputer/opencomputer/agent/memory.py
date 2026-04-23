"""
Three-pillar memory manager.

- Declarative: MEMORY.md + USER.md (plain markdown the user/agent edit)
- Procedural:  ~/.opencomputer/skills/*/SKILL.md (skills folder)
- Episodic:    SQLite + FTS5 (via SessionDB, not here)

This module owns the declarative + procedural reads/writes. Episodic memory
is queried through SessionDB in state.py.

Write-path invariants for MEMORY.md / USER.md:
  - Every mutation goes through ``_write_atomic()``: file lock + write to
    ``<path>.tmp`` + ``os.replace()``. The original is never partially
    overwritten.
  - Before every mutation, the current file is copied to ``<path>.bak`` so
    ``restore_backup()`` can undo one step.
  - Character limits (``memory_char_limit`` / ``user_char_limit``) are
    enforced at write time. Over-limit writes raise ``MemoryTooLargeError``.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import frontmatter

# ─── exceptions ───────────────────────────────────────────────────────


class MemoryTooLargeError(ValueError):
    """Raised when a write would exceed the configured character limit."""

    def __init__(self, kind: str, would_be: int, limit: int) -> None:
        self.kind = kind
        self.would_be = would_be
        self.limit = limit
        super().__init__(
            f"{kind} write would make file {would_be} chars (limit {limit}). "
            f"Use Memory(action='remove',...) or `opencomputer memory prune` first."
        )


# ─── dataclasses ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SkillMeta:
    """Lightweight skill metadata — from frontmatter, without loading the body."""

    id: str
    name: str
    description: str
    path: Path
    version: str = "0.1.0"


# ─── atomic-write + locking helpers ───────────────────────────────────


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Cross-platform exclusive lock on *path*'s directory via a sidecar .lock file.

    POSIX: ``fcntl.flock`` on the lock file.
    Windows: ``msvcrt.locking`` on the same.
    The lock file is kept on disk; it's cheap and makes the lock debuggable.
    """
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Open in a+ so the file is created on first use and not truncated
    # between invocations.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt  # type: ignore[import-not-found]

            # Lock 1 byte from offset 0 — enough for mutual exclusion.
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
    finally:
        os.close(fd)


def _write_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* atomically. Must be called inside _file_lock()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _backup_path(path: Path) -> Path:
    return Path(str(path) + ".bak")


# ─── memory manager ───────────────────────────────────────────────────


class MemoryManager:
    """Reads + mutates declarative memory; lists procedural (skill) memory.

    Skills are searched across multiple roots (kimi-cli pattern):
      1. User skills: ~/.opencomputer/skills/   (write target for new skills)
      2. Bundled skills: <repo>/opencomputer/skills/ (read-only, shipped defaults)

    Higher-priority roots shadow lower-priority ones by skill id.
    """

    def __init__(
        self,
        declarative_path: Path,
        skills_path: Path,
        *,
        user_path: Path | None = None,
        soul_path: Path | None = None,
        memory_char_limit: int = 4000,
        user_char_limit: int = 2000,
        bundled_skills_paths: list[Path] | None = None,
    ) -> None:
        self.declarative_path = declarative_path
        self.user_path = user_path if user_path is not None else declarative_path.parent / "USER.md"
        # Phase 14.F / C3 — optional per-profile personality file. Defaults
        # to ``SOUL.md`` alongside MEMORY.md so existing constructions keep
        # working (absent file → empty string).
        self.soul_path = (
            soul_path if soul_path is not None else declarative_path.parent / "SOUL.md"
        )
        self.skills_path = skills_path
        self.skills_path.mkdir(parents=True, exist_ok=True)
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # Always include bundled skills shipped with core at the lowest priority
        if bundled_skills_paths is None:
            bundled = Path(__file__).resolve().parent.parent / "skills"
            bundled_skills_paths = [bundled] if bundled.exists() else []
        self.bundled_skills_paths = bundled_skills_paths

    # ─── declarative (MEMORY.md) ───────────────────────────────────

    def read_declarative(self) -> str:
        if not self.declarative_path.exists():
            return ""
        return self.declarative_path.read_text(encoding="utf-8")

    def append_declarative(self, text: str) -> None:
        self._append(
            self.declarative_path,
            text,
            limit=self.memory_char_limit,
            kind="memory",
        )

    def replace_declarative(self, old: str, new: str) -> bool:
        return self._replace(
            self.declarative_path,
            old,
            new,
            limit=self.memory_char_limit,
            kind="memory",
        )

    def remove_declarative(self, block: str) -> bool:
        return self._remove(self.declarative_path, block, kind="memory")

    # ─── user profile (USER.md) ────────────────────────────────────

    def read_user(self) -> str:
        if not self.user_path.exists():
            return ""
        return self.user_path.read_text(encoding="utf-8")

    def append_user(self, text: str) -> None:
        self._append(
            self.user_path,
            text,
            limit=self.user_char_limit,
            kind="user",
        )

    def replace_user(self, old: str, new: str) -> bool:
        return self._replace(
            self.user_path,
            old,
            new,
            limit=self.user_char_limit,
            kind="user",
        )

    def remove_user(self, block: str) -> bool:
        return self._remove(self.user_path, block, kind="user")

    # ─── personality (SOUL.md) — Phase 14.F / C3 ──────────────────

    def read_soul(self) -> str:
        """Return the contents of ``SOUL.md`` or '' if absent/unreadable.

        Read-only by design. The profile's personality file is hand-edited
        by the user, not mutated by the agent. Returning '' when the file
        doesn't exist means prompt construction degrades gracefully: no
        profile → no ``## Profile identity`` section.
        """
        if not self.soul_path.exists():
            return ""
        try:
            return self.soul_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    # ─── backup / restore ──────────────────────────────────────────

    def restore_backup(self, which: Literal["memory", "user"]) -> bool:
        """Swap <path>.bak into <path>. Returns True if restored, False if no backup."""
        target = self.declarative_path if which == "memory" else self.user_path
        backup = _backup_path(target)
        if not backup.exists():
            return False
        with _file_lock(target):
            shutil.copy2(backup, target)
        return True

    # ─── stats ─────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "memory_chars": len(self.read_declarative()),
            "memory_char_limit": self.memory_char_limit,
            "user_chars": len(self.read_user()),
            "user_char_limit": self.user_char_limit,
            "memory_path": str(self.declarative_path),
            "user_path": str(self.user_path),
        }

    # ─── shared write helpers ──────────────────────────────────────

    def _append(self, path: Path, text: str, *, limit: int, kind: str) -> None:
        with _file_lock(path):
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            separator = "\n\n" if existing and not existing.endswith("\n\n") else ""
            new_text = existing + separator + text.strip() + "\n"
            if len(new_text) > limit:
                raise MemoryTooLargeError(kind, len(new_text), limit)
            # Backup current state before mutating.
            if path.exists():
                shutil.copy2(path, _backup_path(path))
            _write_atomic(path, new_text)

    def _replace(self, path: Path, old: str, new: str, *, limit: int, kind: str) -> bool:
        with _file_lock(path):
            if not path.exists():
                return False
            existing = path.read_text(encoding="utf-8")
            if old not in existing:
                return False
            candidate = existing.replace(old, new)
            if len(candidate) > limit:
                raise MemoryTooLargeError(kind, len(candidate), limit)
            shutil.copy2(path, _backup_path(path))
            _write_atomic(path, candidate)
            return True

    def _remove(self, path: Path, block: str, *, kind: str) -> bool:
        with _file_lock(path):
            if not path.exists():
                return False
            existing = path.read_text(encoding="utf-8")
            if block not in existing:
                return False
            candidate = existing.replace(block, "")
            # Collapse resulting blank triples.
            while "\n\n\n" in candidate:
                candidate = candidate.replace("\n\n\n", "\n\n")
            shutil.copy2(path, _backup_path(path))
            _write_atomic(path, candidate.lstrip("\n"))
            return True

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


__all__ = ["MemoryManager", "SkillMeta", "MemoryTooLargeError"]
