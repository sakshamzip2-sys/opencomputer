"""Hierarchical instruction-file discovery (CC §3).

Walks the cwd upward to locate every project-rooted instruction file
that should contribute to the system prompt:

  - ``OPENCOMPUTER.md`` (preferred) or ``CLAUDE.md`` or ``AGENTS.md``
    at each level — most-specific wins per directory (in that priority
    order).
  - ``OPENCOMPUTER.local.md`` (gitignored sibling) at each level — adds
    on top of the same-directory base file, never replaces it.
  - ``<profile_home>/rules/*.md`` — global rule files split into
    separate per-topic files (e.g. ``formatting.md``,
    ``security.md``). Loaded first so workspace files override.

Spec: docs/OC-FROM-CLAUDE-CODE.md §3.

Loading order (root → leaf so the leaf can override):

  1. ``<profile_home>/rules/*.md`` (sorted lexically)
  2. Closest git-repo root (or home dir if no git repo): root-level file
  3. Each directory along the walk DOWN from root to cwd: that
     directory's file (if any)
  4. ``OPENCOMPUTER.local.md`` at each level: appears after its base
     same-dir file (override semantics)

Stop conditions for the upward walk:

  - Hit a directory containing ``.git/`` (repo root)
  - Hit the user's home directory
  - Hit the filesystem root

Safe by default:

  - Returns ``[]`` for unreadable / non-existent inputs
  - Empty files are skipped (a 0-byte CLAUDE.md is not "instructions")
  - Each file is read with ``errors="replace"`` so a non-UTF-8 byte
    doesn't crash discovery
  - Read size capped at :data:`MAX_FILE_BYTES` (256 KB) per file — a
    pathological 50 MB CLAUDE.md is truncated with a trailing note
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger(__name__)

#: Names checked at each level, in priority order. The FIRST one found
#: in a directory wins for that directory; later names are skipped.
#: ``OPENCOMPUTER.md`` is the canonical OC name; ``CLAUDE.md`` is the
#: Claude Code parity name; ``AGENTS.md`` is the generic standardized
#: name. A project may have only one of these per directory.
_BASE_FILE_NAMES: tuple[str, ...] = ("OPENCOMPUTER.md", "CLAUDE.md", "AGENTS.md")

#: Per-level override file. Always gitignored. Loaded AFTER its base
#: same-dir file so the override semantics are correct.
_LOCAL_OVERRIDE_NAME: str = "OPENCOMPUTER.local.md"

#: Per-file read cap. Keeps the system prompt from blowing up on
#: pathological instruction files. Anything over this is truncated.
MAX_FILE_BYTES: int = 256 * 1024  # 256 KB

#: How many parent levels to walk upward at most. Defence against
#: pathological filesystem topologies (broken symlink loops). Real
#: repos rarely exceed 8 levels of nesting.
_MAX_PARENT_WALK: int = 32


@dataclass(frozen=True)
class InstructionFile:
    """One instruction file located by hierarchical discovery.

    Attributes:
        path: Absolute path the file was read from.
        content: File body, ``"utf-8"`` decoded with ``errors="replace"``.
            Truncated to :data:`MAX_FILE_BYTES` with a trailing note when
            the source was larger.
        source: Provenance tier — one of:
            * ``"global-rules"`` — from ``<profile_home>/rules/*.md``
            * ``"workspace"``    — base file at a project-tree level
            * ``"local"``        — ``OPENCOMPUTER.local.md`` override
        depth: Distance below the discovered project root (0 = root,
            1 = first subdir, ...). For ``"global-rules"`` source this
            is always 0.
    """

    path: Path
    content: str
    source: str
    depth: int


def _read_capped(path: Path) -> str | None:
    """Read a file with size cap and bad-byte tolerance. Returns
    ``None`` for missing / unreadable / empty files."""
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size <= 0:
            return None
        if size > MAX_FILE_BYTES:
            with path.open("rb") as f:
                head = f.read(MAX_FILE_BYTES)
            text = head.decode("utf-8", errors="replace")
            text += (
                f"\n\n... (truncated; file was {size:,} bytes, capped at "
                f"{MAX_FILE_BYTES:,})\n"
            )
            return text
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError) as exc:
        _LOG.debug("instructions_hierarchy: failed to read %s: %s", path, exc)
        return None


def _find_base_file(directory: Path) -> Path | None:
    """Return the FIRST of ``_BASE_FILE_NAMES`` found in ``directory``,
    or ``None`` when none exist."""
    for name in _BASE_FILE_NAMES:
        candidate = directory / name
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _find_repo_root(start: Path, home: Path | None) -> Path:
    """Walk upward from ``start`` to the first directory containing
    ``.git/``. Stops at ``home`` (returned as the root) when no git
    repo is found before that. Stops at the filesystem root in the
    worst case.

    Always returns an absolute directory ``Path``. Symlink-resolved
    so ``..`` cycles can't bind us.
    """
    try:
        start_resolved = start.resolve(strict=False)
    except (OSError, RuntimeError):
        return start
    home_resolved = None
    if home is not None:
        try:
            home_resolved = home.resolve(strict=False)
        except (OSError, RuntimeError):
            home_resolved = home

    cursor = start_resolved if start_resolved.is_dir() else start_resolved.parent
    for _ in range(_MAX_PARENT_WALK):
        try:
            if (cursor / ".git").exists():
                return cursor
        except OSError:
            pass
        if home_resolved is not None and cursor == home_resolved:
            return cursor
        parent = cursor.parent
        if parent == cursor:
            return cursor
        cursor = parent
    return cursor


def _list_global_rules(profile_home: Path | None) -> list[InstructionFile]:
    """Return every ``*.md`` under ``<profile_home>/rules/``, sorted
    lexically so the loading order is deterministic across runs.
    Empty list when the dir doesn't exist."""
    if profile_home is None:
        return []
    rules_dir = profile_home / "rules"
    try:
        if not rules_dir.is_dir():
            return []
        md_paths = sorted(p for p in rules_dir.iterdir() if p.suffix == ".md")
    except OSError:
        return []
    out: list[InstructionFile] = []
    for p in md_paths:
        content = _read_capped(p)
        if content is None or not content.strip():
            continue
        out.append(
            InstructionFile(path=p, content=content, source="global-rules", depth=0)
        )
    return out


def _walk_levels(root: Path, leaf: Path) -> list[Path]:
    """Return the chain of directories from ``root`` down to ``leaf``,
    inclusive of both. When ``leaf`` is not a descendant of ``root``,
    returns ``[root]``. The walk is descending (root first) so callers
    get the right load order to apply later-overrides-earlier.
    """
    try:
        root_resolved = root.resolve(strict=False)
        leaf_resolved = leaf.resolve(strict=False)
    except (OSError, RuntimeError):
        return [root]
    if leaf_resolved == root_resolved:
        return [root_resolved]
    try:
        rel = leaf_resolved.relative_to(root_resolved)
    except ValueError:
        return [root_resolved]
    parts = rel.parts
    levels: list[Path] = [root_resolved]
    cursor = root_resolved
    for part in parts:
        cursor = cursor / part
        try:
            if cursor.is_dir():
                levels.append(cursor)
        except OSError:
            break
    return levels


def find_hierarchical_instructions(
    cwd: Path | None = None,
    profile_home: Path | None = None,
) -> list[InstructionFile]:
    """Discover instruction files contributing to the system prompt.

    Args:
        cwd: Directory to start the upward walk from. Defaults to
            ``Path.cwd()``. A non-directory ``cwd`` is treated as
            ``cwd.parent``.
        profile_home: OC profile home (e.g. ``~/.opencomputer/coder/``).
            Used to locate the ``rules/`` directory. ``None`` skips
            global rules entirely.

    Returns:
        A list of :class:`InstructionFile` in load order:
        global-rules (alphabetical) → root file → each level's file
        from root-to-leaf → ``.local.md`` overrides at each level
        appear immediately after their base file at the same level.

        The leaf-most files appear last so they override root-level
        rules when the system prompt builder concatenates them.

        Returns ``[]`` for completely empty discovery — there's never
        a built-in fallback.

    Safe by default — every error path swallows + returns the
    partial result rather than raising.
    """
    if cwd is None:
        try:
            cwd = Path(os.getcwd())
        except (FileNotFoundError, OSError):
            return []
    cwd = cwd if cwd.is_dir() else cwd.parent

    home: Path | None
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        home = None

    out: list[InstructionFile] = []
    # 1. Global rules first — they're the most general; project files override.
    out.extend(_list_global_rules(profile_home))

    # 2. Locate repo root, then load files from root → leaf.
    root = _find_repo_root(cwd, home)
    levels = _walk_levels(root, cwd)
    for depth, directory in enumerate(levels):
        # Base file: first matching name wins for this directory.
        base = _find_base_file(directory)
        if base is not None:
            content = _read_capped(base)
            if content and content.strip():
                out.append(
                    InstructionFile(
                        path=base, content=content, source="workspace", depth=depth
                    )
                )
        # Local override: always sits next to the base file (or alone).
        local = directory / _LOCAL_OVERRIDE_NAME
        try:
            local_exists = local.is_file()
        except OSError:
            local_exists = False
        if local_exists:
            content = _read_capped(local)
            if content and content.strip():
                out.append(
                    InstructionFile(
                        path=local, content=content, source="local", depth=depth
                    )
                )
    return out


def format_for_system_prompt(files: list[InstructionFile]) -> str:
    """Render the discovered files into a single string suitable for
    inlining into the system prompt.

    Each file is preceded by a comment line naming its path + source
    so the model can attribute rules and so debugging is easy
    (``/context`` / ``oc context show`` could surface this verbatim).

    Empty file list → empty string (caller need not branch).
    """
    if not files:
        return ""
    parts: list[str] = []
    for f in files:
        header = f"<!-- {f.source}:{f.path} -->"
        parts.append(f"{header}\n{f.content.rstrip()}")
    return "\n\n".join(parts) + "\n"


__all__ = [
    "InstructionFile",
    "MAX_FILE_BYTES",
    "find_hierarchical_instructions",
    "format_for_system_prompt",
]
