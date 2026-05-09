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

import logging
import os
import re
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import frontmatter

logger = logging.getLogger("opencomputer.agent.memory")

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
class SkillReference:
    """A single on-demand reference or worked example attached to a skill.

    Claude Code's directory-hierarchy skill layout stores deep content in
    sibling ``references/*.md`` and ``examples/*`` directories so the main
    SKILL.md stays concise. ``SkillReference`` represents one such file.

    - ``path``    — absolute path to the reference file on disk.
    - ``title``   — derived from (in priority order) frontmatter
                    ``title``/``name`` → first ``# Heading`` line → filename
                    stem. Used for prompt-injection labelling.
    - ``content`` — full file contents as text. Eager-loaded at scan time
                    to keep the dataclass frozen and hashable; the whole
                    corpus is only ~16 skills × a handful of files, so the
                    cost is negligible at startup.
    """

    path: Path
    title: str
    content: str


# Alias: worked examples share the same shape as references. Exposed as a
# distinct name so callers can type-hint ``SkillExample`` when they mean
# "this is a worked example" vs "this is reference documentation".
SkillExample = SkillReference


@dataclass(frozen=True, slots=True)
class RequiredEnvVar:
    """A skill-declared environment-variable dependency (Hermes parity).

    Mirrors the Hermes ``required_environment_variables`` SKILL.md
    frontmatter shape. When a skill is loaded, each declared var is
    auto-registered for passthrough into ExecuteCode + sandbox subprocesses
    and the user is prompted to supply it (via ``oc setup`` /
    ``oc skills env``) if it isn't already in the environment.

    Attributes:
        name: env var key (e.g. ``TENOR_API_KEY``).
        prompt: short label shown in the setup prompt
            (e.g. ``"Tenor API key"``).
        help: optional URL or text pointing to where the user can
            obtain the value.
    """

    name: str
    prompt: str = ""
    help: str = ""


@dataclass(frozen=True, slots=True)
class RequiredCredentialFile:
    """A skill-declared credential-file dependency (Hermes parity).

    Mirrors ``required_credential_files`` SKILL.md frontmatter. When the
    Docker sandbox spawns a process, each declared file is bind-mounted
    read-only into ``/root/.opencomputer/<path>`` so OAuth tokens and
    similar long-lived credentials are visible inside the container
    without having to re-pair every run.

    Attributes:
        path: relative path under ``~/.opencomputer/`` (e.g.
            ``google_token.json``).
        description: human description shown when the file is missing.
    """

    path: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class SkillMeta:
    """Lightweight skill metadata — from frontmatter, without loading the body.

    Phase III.4 extends this with ``references`` + ``examples`` tuples to
    support Claude Code's directory-hierarchy skill layout. Flat
    single-file SKILL.md skills get empty tuples for both — the behaviour
    is unchanged from their perspective.

    Hermes-parity (P3.4): ``required_env_vars`` + ``required_credential_files``
    declare passthrough requirements. They land empty for skills that don't
    set the frontmatter keys — fully backward compatible.
    """

    id: str
    name: str
    description: str
    path: Path
    version: str = "0.1.0"
    references: tuple[SkillReference, ...] = field(default_factory=tuple)
    examples: tuple[SkillReference, ...] = field(default_factory=tuple)
    #: v0.5+: priority weight from frontmatter ``priority:`` key.
    #: Higher = surfaced earlier. None = unweighted (alphabetical fallback).
    #: Future engines may auto-update this based on outcome data.
    priority: float | None = None
    #: P3.4 Hermes parity: skill-declared environment-var passthrough.
    required_env_vars: tuple[RequiredEnvVar, ...] = field(default_factory=tuple)
    #: P3.5 Hermes parity: skill-declared credential-file bind mounts.
    required_credential_files: tuple[RequiredCredentialFile, ...] = field(default_factory=tuple)


# ─── Hermes-parity skill-frontmatter parsers (P3.4 + P3.5) ────────────


def _parse_required_env_vars(raw: object) -> tuple[RequiredEnvVar, ...]:
    """Parse the ``required_environment_variables`` frontmatter key.

    Accepts:
        - list of dicts ``[{name: X, prompt: Y, help: Z}, ...]``
        - list of bare strings ``[X, Y, Z]`` — name only
        - any other shape → empty tuple

    Empty-name entries are dropped silently (a malformed skill must
    never break the loader for the others).
    """
    if not isinstance(raw, list):
        return ()
    out: list[RequiredEnvVar] = []
    for entry in raw:
        if isinstance(entry, str):
            name = entry.strip()
            if name:
                out.append(RequiredEnvVar(name=name))
        elif isinstance(entry, dict):
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            out.append(
                RequiredEnvVar(
                    name=name,
                    prompt=str(entry.get("prompt", "") or ""),
                    help=str(entry.get("help", "") or ""),
                )
            )
        # Anything else: skip silently.
    return tuple(out)


def _parse_required_credential_files(raw: object) -> tuple[RequiredCredentialFile, ...]:
    """Parse the ``required_credential_files`` frontmatter key.

    Accepts list of dicts ``[{path: ..., description: ...}, ...]`` or
    list of bare strings (path only). Non-list / malformed → empty tuple.
    """
    if not isinstance(raw, list):
        return ()
    out: list[RequiredCredentialFile] = []
    for entry in raw:
        if isinstance(entry, str):
            path = entry.strip()
            if path:
                out.append(RequiredCredentialFile(path=path))
        elif isinstance(entry, dict):
            path = str(entry.get("path", "")).strip()
            if not path:
                continue
            out.append(
                RequiredCredentialFile(
                    path=path,
                    description=str(entry.get("description", "") or ""),
                )
            )
    return tuple(out)


# ─── bus helper (T3.2 PR-8) ───────────────────────────────────────────


def _publish_memory_write_event(*, action: str, target: str, content_size: int) -> None:
    """Publish a MemoryWriteEvent to the default bus. Exception-isolated.

    Called after each successful declarative-memory write so MemoryBridge
    subscribers can trigger provider callbacks (audit pattern). Content
    itself is NOT included — only action, target name, and size.
    """
    try:
        from opencomputer.ingestion.bus import default_bus
        from plugin_sdk.ingestion import MemoryWriteEvent

        default_bus.publish(MemoryWriteEvent(
            session_id=None,
            source="agent_memory",
            action=action,
            target=target,
            content_size=content_size,
        ))
    except Exception:  # noqa: BLE001 — must never break a memory write path
        pass


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


# ─── skill-hierarchy helpers (III.4) ──────────────────────────────────


# Match only the first ``# `` heading line, ignoring leading blank lines or
# frontmatter. Used to derive a reference's display title when no
# frontmatter ``title``/``name`` is present.
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _derive_reference_title(path: Path, raw_text: str) -> str:
    """Pick a human-readable label for a reference/example file.

    Priority:
      1. Frontmatter ``title`` or ``name`` (if the file parses as a
         markdown-with-frontmatter post).
      2. First ``# Heading`` line in the body.
      3. Filename stem (e.g. ``alpha.md`` → ``alpha``).
    """
    # Try frontmatter first — many reference files under claude-code's
    # plugin-dev skills carry frontmatter of their own.
    try:
        post = frontmatter.loads(raw_text)
        fm_title = post.metadata.get("title") or post.metadata.get("name")
        if isinstance(fm_title, str) and fm_title.strip():
            return fm_title.strip()
        body = post.content
    except Exception:  # noqa: BLE001 — any parse failure falls through
        body = raw_text

    m = _H1_RE.search(body)
    if m:
        return m.group(1).strip()
    return path.stem


def _load_references_dir(
    subdir: Path, *, markdown_only: bool
) -> tuple[SkillReference, ...]:
    """Enumerate files in a subdir and build SkillReference tuples.

    ``references/`` accepts only ``*.md`` — structured documentation.
    ``examples/`` accepts any file type (``.md``, ``.py``, ``.json``,
    ``.yaml``, ...) and reads each as text.

    Non-text files (e.g. images accidentally dropped under examples/)
    that fail UTF-8 decoding are silently skipped rather than crashing
    the loader.

    Entries are sorted by filename so prompt injection is deterministic.
    """
    if not subdir.is_dir():
        return ()

    out: list[SkillReference] = []
    for child in sorted(subdir.iterdir()):
        if not child.is_file():
            continue
        if markdown_only and child.suffix.lower() != ".md":
            continue
        try:
            raw = child.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        title = _derive_reference_title(child, raw)
        out.append(SkillReference(path=child, title=title, content=raw))
    return tuple(out)


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
        global_soul_path: Path | None = None,
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
        # Hermes v2 D4 (2026-05-08) — optional global SOUL.md fallback.
        # When the per-profile soul_path is missing/empty, ``read_soul``
        # consults this path instead. Defaults to
        # ``~/.opencomputer/SOUL.md`` (sibling of all per-profile
        # directories), respecting the ``OPENCOMPUTER_HOME`` env var so
        # tests + alt configurations can override.
        if global_soul_path is None:
            home_root = os.environ.get(
                "OPENCOMPUTER_HOME",
                str(Path.home() / ".opencomputer"),
            )
            global_soul_path = Path(home_root) / "SOUL.md"
        self.global_soul_path = global_soul_path
        self.skills_path = skills_path
        self.skills_path.mkdir(parents=True, exist_ok=True)
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # Always include bundled skills shipped with core at the lowest priority
        if bundled_skills_paths is None:
            bundled = Path(__file__).resolve().parent.parent / "skills"
            bundled_skills_paths = [bundled] if bundled.exists() else []
        self.bundled_skills_paths = bundled_skills_paths

        # v1.1 plan-3 M6.1 — BM25 index over MEMORY.md.  Lazy-built; cache
        # under <profile_home>/cache/.  Invalidated on every successful
        # declarative write below.
        from opencomputer.agent.memory_index import BM25Index

        self._bm25_index = BM25Index(self.declarative_path.parent)

    @property
    def bm25_index(self) -> "BM25Index":  # type: ignore[name-defined]  # forward-ref string keeps the import lazy
        """BM25 retrieval index over MEMORY.md (v1.1 plan-3 M6.1)."""
        return self._bm25_index

    def rebind_to_profile(self, profile_home: Path) -> None:
        """Re-resolve declarative_path / user_path / soul_path to point at
        a new profile's home directory. Used by the Ctrl+P profile-swap
        flow to make subsequent read_* calls hit the new profile's
        SOUL.md / MEMORY.md / USER.md without recreating the manager.

        ``skills_path``, bundled-skills paths, and ``global_soul_path``
        are NOT rebound — skill roots and the global SOUL fallback are
        shared across profiles, not per-profile.

        The per-profile BM25 index is swapped to point at the new home so
        retrieval isolates cleanly across profiles.
        """
        self.declarative_path = profile_home / "MEMORY.md"
        self.user_path = profile_home / "USER.md"
        self.soul_path = profile_home / "SOUL.md"

        from opencomputer.agent.memory_index import BM25Index

        self._bm25_index = BM25Index(profile_home)

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
        self._bm25_index.invalidate()

    def replace_declarative(self, old: str, new: str) -> bool:
        changed = self._replace(
            self.declarative_path,
            old,
            new,
            limit=self.memory_char_limit,
            kind="memory",
        )
        if changed:
            self._bm25_index.invalidate()
        return changed

    def remove_declarative(self, block: str) -> bool:
        changed = self._remove(self.declarative_path, block, kind="memory")
        if changed:
            self._bm25_index.invalidate()
        return changed

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
        """Return SOUL.md text — per-profile preferred, global fallback, '' otherwise.

        Resolution order (Hermes v2 D4, 2026-05-08):

        1. Per-profile ``self.soul_path`` (e.g. ``~/.opencomputer/coder/SOUL.md``).
           Used if it exists and has non-whitespace content.
        2. Global ``self.global_soul_path`` (e.g. ``~/.opencomputer/SOUL.md``).
           Used as fallback when per-profile is missing/empty. Mirrors
           Hermes' ``HERMES_HOME/SOUL.md`` behavior — a single identity
           shared across profiles unless the profile explicitly overrides.
        3. ``""`` — falls back to base.j2's built-in identity preamble
           per Hermes v2: "Empty/whitespace-only file → falls back to
           built-in default identity".

        Read-only by design. Each candidate is treated as missing if it
        doesn't exist, fails to read, or contains only whitespace.
        """
        for candidate in (self.soul_path, self.global_soul_path):
            content = self._read_soul_candidate(candidate)
            if content:
                return content
        return ""

    def _read_soul_candidate(self, path: Path) -> str:
        """Read one SOUL.md candidate. Returns '' if missing/unreadable/empty."""
        if not path.exists():
            return ""
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        if not content.strip():
            return ""
        return content

    # ─── backup / restore ──────────────────────────────────────────

    def restore_backup(self, which: Literal["memory", "user"]) -> bool:
        """Swap <path>.bak into <path>. Returns True if restored, False if no backup."""
        target = self.declarative_path if which == "memory" else self.user_path
        backup = _backup_path(target)
        if not backup.exists():
            return False
        with _file_lock(target):
            shutil.copy2(backup, target)
        if which == "memory":
            self._bm25_index.invalidate()
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
        # T3.2 (PR-8): publish MemoryWriteEvent after the lock releases.
        # Privacy: content_size only, NOT the content itself.
        _publish_memory_write_event(
            action="append", target=path.name, content_size=len(new_text)
        )

    def _replace(self, path: Path, old: str, new: str, *, limit: int, kind: str) -> bool:
        replaced = False
        candidate_size = 0
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
            replaced = True
            candidate_size = len(candidate)
        if replaced:
            # T3.2 (PR-8): publish MemoryWriteEvent after lock releases.
            _publish_memory_write_event(
                action="replace", target=path.name, content_size=candidate_size
            )
        return replaced

    def _remove(self, path: Path, block: str, *, kind: str) -> bool:
        removed = False
        candidate_size = 0
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
            final = candidate.lstrip("\n")
            _write_atomic(path, final)
            removed = True
            candidate_size = len(final)
        if removed:
            # T3.2 (PR-8): publish MemoryWriteEvent after lock releases.
            _publish_memory_write_event(
                action="remove", target=path.name, content_size=candidate_size
            )
        return removed

    # ─── procedural (skills) ─────────────────────────────────────

    def list_skills(self) -> list[SkillMeta]:
        """Scan all skill roots for SKILL.md files. User skills shadow bundled ones.

        Phase III.4: also enumerates sibling ``references/`` (``.md`` only)
        and ``examples/`` (any file type, read as text) subdirs when
        present, populating ``SkillMeta.references`` + ``SkillMeta.examples``.
        A skill directory missing ``SKILL.md`` is silently skipped — including
        when it only has a lone ``references/`` subdir (treated as an
        incomplete skill, not an error).

        Skills Hub (Tier 1.A): each subdirectory of ``<skills_path>/.hub/``
        is treated as an additional root, so hub-installed skills at
        ``<skills_path>/.hub/<source>/<skill-name>/SKILL.md`` are discovered.
        Hub roots are appended after user + bundled roots, so user skills
        still shadow on id collision.
        """
        roots = [self.skills_path, *self.bundled_skills_paths]
        hub_root = self.skills_path / ".hub"
        if hub_root.is_dir():
            for source_dir in sorted(hub_root.iterdir()):
                if source_dir.is_dir():
                    roots.append(source_dir)
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
                references = _load_references_dir(
                    skill_dir / "references", markdown_only=True
                )
                examples = _load_references_dir(
                    skill_dir / "examples", markdown_only=False
                )
                # v0.5+: priority is optional. Frontmatter accepts a
                # numeric value; non-numeric or missing → None (unweighted).
                priority_raw = meta.get("priority")
                priority: float | None
                try:
                    priority = (
                        float(priority_raw) if priority_raw is not None else None
                    )
                except (TypeError, ValueError):
                    priority = None
                # P3.4 + P3.5 Hermes parity: parse required_env_vars +
                # required_credential_files out of frontmatter. Each is
                # tolerant of either a list of dicts (preferred shape)
                # or a list of bare strings (Hermes accepts both for
                # env vars). Malformed entries are skipped silently to
                # match existing skill-loader resilience posture (a
                # broken skill must never starve other skills' load).
                required_env = _parse_required_env_vars(
                    meta.get("required_environment_variables")
                )
                required_creds = _parse_required_credential_files(
                    meta.get("required_credential_files")
                )
                out.append(
                    SkillMeta(
                        id=skill_dir.name,
                        name=str(meta.get("name", skill_dir.name)),
                        description=str(meta.get("description", "")),
                        path=skill_md,
                        version=str(meta.get("version", "0.1.0")),
                        references=references,
                        examples=examples,
                        priority=priority,
                        required_env_vars=required_env,
                        required_credential_files=required_creds,
                    )
                )
                # Hermes parity (P3.4 / P3.5): publish the skill's
                # declared requirements to the global passthrough
                # registry. ExecuteCode + sandbox.docker + setup
                # wizard consult that registry. Failure here must not
                # break skill enumeration — a registry bug shouldn't
                # starve the rest of the agent.
                try:
                    from opencomputer.security import env_passthrough

                    env_passthrough.register_skill_requirements(
                        skill_dir.name,
                        env_vars=required_env,
                        credential_files=required_creds,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "env_passthrough register failed for skill %s",
                        skill_dir.name, exc_info=True,
                    )
        # v0.5+: stable sort by (priority DESC NULLS LAST, name ASC).
        # Skills without priority retain alphabetical ordering — zero
        # behavior change for v0 skills that don't set the field.
        out.sort(
            key=lambda s: (
                -s.priority if s.priority is not None else float("inf"),
                s.name,
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


__all__ = [
    "MemoryManager",
    "MemoryTooLargeError",
    "SkillExample",
    "SkillMeta",
    "SkillReference",
]
