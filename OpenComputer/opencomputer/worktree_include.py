"""Worktree-include — copy gitignored files into a session worktree.

When ``oc code -w`` creates a fresh git worktree, the worktree's tree
mirrors HEAD — so gitignored runtime files (``.env``, ``.venv/``,
``node_modules/``) are MISSING from the working dir. The agent that
lands inside cannot run tests, hit external APIs, or use installed
deps.

This module reads ``<repo_root>/.worktreeinclude`` (gitignore-style
patterns) and copies the matched paths into the worktree, preserving
relative structure, mode, and mtime. Failures on individual files do
NOT abort the entire copy; instead they're recorded in ``CopyReport``.

Resolution order:
  1. ``<repo_root>/.worktreeinclude``       (project-specific)
  2. ``<profile_home>/worktreeinclude``     (global fallback; opt-out via
                                              ``worktree.include_global_fallback=false``)

See ``OpenComputer/docs/superpowers/specs/2026-05-08-worktree-include-checkpoint-hygiene-design.md``
section A for the full design.
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("opencomputer.worktree.include")


@dataclass(frozen=True, slots=True)
class CopyEntry:
    """One source→destination copy result."""

    src: Path
    dst: Path
    bytes_copied: int


@dataclass(frozen=True, slots=True)
class CopyReport:
    """Summary of a :func:`copy_into_worktree` run.

    Attributes:
        copied: per-file successes (one entry per file even when the
            source was a directory).
        skipped: ``(src, reason)`` pairs for files we deliberately did
            not copy (size cap, symlink cycle, etc.).
        failed: ``(src, error_string)`` pairs for files we tried to copy
            but an :class:`OSError` or :class:`PermissionError`
            prevented it.
        total_bytes: sum of ``bytes_copied`` across ``copied``.
        dry_run: ``True`` when the run was a dry-run (no I/O occurred).
    """

    copied: tuple[CopyEntry, ...] = ()
    skipped: tuple[tuple[Path, str], ...] = ()
    failed: tuple[tuple[Path, str], ...] = ()
    total_bytes: int = 0
    dry_run: bool = False


class WorktreeIncludeTooLargeError(RuntimeError):
    """Raised when total bytes to copy exceed ``worktree.include_max_total_mb``.

    The caller is expected to remove the partial worktree and surface
    the error to the user — silent half-populated worktrees are worse
    than a clear failure.
    """

    def __init__(
        self,
        *,
        total_bytes: int,
        cap_bytes: int,
        oversize_paths: tuple[Path, ...],
    ) -> None:
        self.total_bytes = total_bytes
        self.cap_bytes = cap_bytes
        self.oversize_paths = oversize_paths
        super().__init__(
            f".worktreeinclude would copy {total_bytes:,} bytes "
            f"(cap is {cap_bytes:,}). "
            f"Largest paths: {[str(p) for p in oversize_paths[:3]]}"
        )


# ─── parse / expand / copy ──────────────────────────────────────────


def parse_worktreeinclude(path: Path) -> list[str]:
    """Parse a ``.worktreeinclude`` file. Gitignore-style.

    Lines are stripped. Lines starting with ``#`` (after strip) are
    treated as comments. Blank lines are ignored. Returns the surviving
    pattern strings in file order.

    Tolerant on missing files (returns ``[]``) and on undecodable UTF-8
    (logs + returns ``[]``). Never raises.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (UnicodeDecodeError, OSError) as exc:
        logger.warning("worktreeinclude: could not read %s: %s — ignoring", path, exc)
        return []

    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        out.append(line)
    return out


def expand_patterns(repo_root: Path, patterns: list[str]) -> list[Path]:
    """Expand each pattern relative to ``repo_root``.

    Patterns may be:

    - a literal file path (``.env``)
    - a literal directory (``.venv/`` — trailing slash optional)
    - a glob (``config/*.local.yaml``)

    A pattern that resolves outside ``repo_root`` is rejected with a
    warning and contributes nothing. The returned list is deduplicated
    and sorted (lexicographic on ``str(path)``).
    """
    seen: set[Path] = set()
    out: list[Path] = []
    repo_root_resolved = repo_root.resolve()

    for raw in patterns:
        # Strip trailing slash for `.venv/` so glob/exists works on the
        # directory itself.
        stripped = raw.rstrip("/")
        try:
            matches: list[Path]
            if any(c in stripped for c in "*?[]"):
                matches = list(repo_root.glob(stripped))
            else:
                candidate = repo_root / stripped
                matches = [candidate] if candidate.exists() else []
        except (OSError, ValueError) as exc:
            logger.warning("worktreeinclude: failed to expand %r: %s", raw, exc)
            continue

        for m in matches:
            try:
                resolved = m.resolve()
            except (OSError, RuntimeError):
                # Broken symlink, etc.
                continue
            # Reject anything outside repo_root (e.g. via "../foo").
            try:
                resolved.relative_to(repo_root_resolved)
            except ValueError:
                logger.warning(
                    "worktreeinclude: skipping %s — escapes repo_root", m
                )
                continue
            if m in seen:
                continue
            seen.add(m)
            out.append(m)

    out.sort(key=str)
    return out


def _measure(path: Path) -> int:
    """Return total bytes for ``path`` (file size, or recursive dir size)."""
    if path.is_symlink():
        # Symlink itself is small; the link target is not measured here.
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for fn in files:
            try:
                total += (Path(root) / fn).stat().st_size
            except OSError:
                pass
    return total


def _atomic_copy_file(src: Path, dst: Path) -> int:
    """Copy a single file atomically (temp + rename). Returns bytes copied."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.tmp.{secrets.token_hex(4)}"
    shutil.copy2(src, tmp, follow_symlinks=False)
    os.replace(tmp, dst)
    try:
        return dst.stat().st_size
    except OSError:
        return 0


def copy_into_worktree(
    sources: list[Path],
    repo_root: Path,
    worktree: Path,
    *,
    dry_run: bool = False,
    max_total_mb: int = 1000,
    max_per_file_mb: int = 500,
    follow_symlinks: bool = False,
) -> CopyReport:
    """Copy each source into the worktree at the same relative path.

    Failures on individual files are logged + recorded in
    ``report.failed`` or ``report.skipped``; the run continues. Cap
    violations of ``max_total_mb`` raise
    :class:`WorktreeIncludeTooLargeError` BEFORE any I/O.

    Args:
        sources: list of paths under ``repo_root``.
        repo_root: the project root (used to compute relative dst).
        worktree: destination root.
        dry_run: when True, no I/O occurs; the report still reflects
            what would have been copied.
        max_total_mb: hard cap on total bytes; exceeding aborts.
        max_per_file_mb: per-file size cap; oversize files are skipped.
        follow_symlinks: when False (default), symlinks copied as links.

    Returns:
        :class:`CopyReport`.
    """
    cap_bytes = max_total_mb * 1024 * 1024
    per_file_cap = max_per_file_mb * 1024 * 1024
    repo_root_resolved = repo_root.resolve()

    # Pre-flight size calc — on cap miss we do not start I/O.
    sizes: dict[Path, int] = {p: _measure(p) for p in sources}
    total = sum(sizes.values())
    if total > cap_bytes:
        oversize = tuple(sorted(sizes, key=lambda p: -sizes[p]))[:5]
        raise WorktreeIncludeTooLargeError(
            total_bytes=total,
            cap_bytes=cap_bytes,
            oversize_paths=oversize,
        )

    copied: list[CopyEntry] = []
    skipped: list[tuple[Path, str]] = []
    failed: list[tuple[Path, str]] = []
    seen_realpaths: set[str] = set()
    total_bytes = 0

    def _enroll_copy(src: Path, dst: Path, n: int) -> None:
        nonlocal total_bytes
        copied.append(CopyEntry(src=src, dst=dst, bytes_copied=n))
        total_bytes += n

    for src in sources:
        try:
            rel = src.relative_to(repo_root_resolved)
        except ValueError:
            try:
                rel = src.resolve().relative_to(repo_root_resolved)
            except ValueError:
                skipped.append((src, "outside repo_root"))
                continue
        dst = worktree / rel

        # Per-file size cap (only meaningful for plain files).
        if src.is_file() and not src.is_symlink():
            if sizes.get(src, _measure(src)) > per_file_cap:
                logger.warning(
                    "worktreeinclude: skipping %s — exceeds max_per_file_mb=%d",
                    src,
                    max_per_file_mb,
                )
                skipped.append((src, f"exceeds max_per_file_mb={max_per_file_mb}"))
                continue

        if dry_run:
            n = sizes.get(src, _measure(src))
            _enroll_copy(src, dst, n)
            continue

        try:
            if src.is_symlink() and not follow_symlinks:
                # Preserve as symlink. Cycle check via realpath set.
                target = os.readlink(src)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                os.symlink(target, dst)
                _enroll_copy(src, dst, 0)
            elif src.is_file():
                n = _atomic_copy_file(src, dst)
                _enroll_copy(src, dst, n)
            elif src.is_dir():
                _copy_directory_recursive(
                    src=src,
                    dst=dst,
                    repo_root_resolved=repo_root_resolved,
                    follow_symlinks=follow_symlinks,
                    per_file_cap=per_file_cap,
                    max_per_file_mb=max_per_file_mb,
                    seen_realpaths=seen_realpaths,
                    copied=copied,
                    skipped=skipped,
                    failed=failed,
                )
                total_bytes = sum(e.bytes_copied for e in copied)
            else:
                skipped.append((src, "neither file nor dir"))
        except (OSError, PermissionError) as exc:
            logger.warning("worktreeinclude: copy failed for %s: %s", src, exc)
            failed.append((src, str(exc)))

    return CopyReport(
        copied=tuple(copied),
        skipped=tuple(skipped),
        failed=tuple(failed),
        total_bytes=total_bytes,
        dry_run=dry_run,
    )


def _copy_directory_recursive(
    *,
    src: Path,
    dst: Path,
    repo_root_resolved: Path,
    follow_symlinks: bool,
    per_file_cap: int,
    max_per_file_mb: int,
    seen_realpaths: set[str],
    copied: list[CopyEntry],
    skipped: list[tuple[Path, str]],
    failed: list[tuple[Path, str]],
) -> None:
    real = str(src.resolve())
    if real in seen_realpaths:
        skipped.append((src, "symlink cycle"))
        return
    seen_realpaths.add(real)

    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        rel_name = entry.name
        sub_dst = dst / rel_name
        try:
            if entry.is_symlink() and not follow_symlinks:
                target = os.readlink(entry)
                if sub_dst.exists() or sub_dst.is_symlink():
                    sub_dst.unlink()
                os.symlink(target, sub_dst)
                copied.append(CopyEntry(src=entry, dst=sub_dst, bytes_copied=0))
            elif entry.is_file():
                if entry.stat().st_size > per_file_cap:
                    logger.warning(
                        "worktreeinclude: skipping %s — exceeds max_per_file_mb=%d",
                        entry,
                        max_per_file_mb,
                    )
                    skipped.append(
                        (entry, f"exceeds max_per_file_mb={max_per_file_mb}")
                    )
                    continue
                n = _atomic_copy_file(entry, sub_dst)
                copied.append(CopyEntry(src=entry, dst=sub_dst, bytes_copied=n))
            elif entry.is_dir():
                _copy_directory_recursive(
                    src=entry,
                    dst=sub_dst,
                    repo_root_resolved=repo_root_resolved,
                    follow_symlinks=follow_symlinks,
                    per_file_cap=per_file_cap,
                    max_per_file_mb=max_per_file_mb,
                    seen_realpaths=seen_realpaths,
                    copied=copied,
                    skipped=skipped,
                    failed=failed,
                )
        except (OSError, PermissionError) as exc:
            logger.warning(
                "worktreeinclude: dir entry copy failed %s: %s", entry, exc
            )
            failed.append((entry, str(exc)))


def apply_to_worktree(
    repo_root: Path,
    worktree: Path,
    *,
    dry_run: bool = False,
    max_total_mb: int = 1000,
    max_per_file_mb: int = 500,
    follow_symlinks: bool = False,
    global_fallback_path: Path | None = None,
) -> CopyReport:
    """Read ``.worktreeinclude`` (project + optional global) and copy.

    Convenience wrapper used by :func:`session_worktree` and the CLI's
    ``oc worktrees include-preview``. Logs an INFO-level summary
    (count + bytes + skipped/failed counts).
    """
    project_file = repo_root / ".worktreeinclude"
    patterns: list[str] = parse_worktreeinclude(project_file)
    if global_fallback_path is not None and global_fallback_path.exists():
        global_patterns = parse_worktreeinclude(global_fallback_path)
        # Project takes precedence on duplicate string match.
        seen_strings = set(patterns)
        for p in global_patterns:
            if p not in seen_strings:
                patterns.append(p)

    if not patterns:
        logger.debug(
            "worktreeinclude: no patterns at %s (global=%s) — skip",
            project_file,
            global_fallback_path,
        )
        return CopyReport(dry_run=dry_run)

    sources = expand_patterns(repo_root, patterns)
    if not sources:
        logger.info(
            "worktreeinclude: %d patterns produced 0 matches — nothing to copy",
            len(patterns),
        )
        return CopyReport(dry_run=dry_run)

    report = copy_into_worktree(
        sources,
        repo_root,
        worktree,
        dry_run=dry_run,
        max_total_mb=max_total_mb,
        max_per_file_mb=max_per_file_mb,
        follow_symlinks=follow_symlinks,
    )
    logger.info(
        "worktreeinclude: %s %d files (%.1f MB), skipped=%d, failed=%d",
        "would-copy" if dry_run else "copied",
        len(report.copied),
        report.total_bytes / (1024 * 1024),
        len(report.skipped),
        len(report.failed),
    )
    return report


__all__ = [
    "CopyEntry",
    "CopyReport",
    "WorktreeIncludeTooLargeError",
    "apply_to_worktree",
    "copy_into_worktree",
    "expand_patterns",
    "parse_worktreeinclude",
]
