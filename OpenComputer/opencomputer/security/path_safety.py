"""Centralised filesystem path-safety checks.

Today's codebase has path-validation logic sprinkled across tools
(``tools/vision_analyze.py:_is_safe_image_path``, ad-hoc
``Path.resolve().is_relative_to(...)`` checks elsewhere). This module
is the canonical home: callers import from here so a single hardening
pass benefits every tool that reads files.

Policy
------

A path is "safe" iff its **resolved absolute form** is contained within
at least one of the configured safe roots. Resolution collapses symlink
traversal and ``..`` traversal — a symlink at
``<safe_root>/link → /etc/shadow`` resolves to ``/etc/shadow`` and is
correctly flagged as unsafe even though the literal path string starts
with the safe root.

Resolve failures (broken symlinks, permission errors) are treated as
unsafe — fail-closed.

Usage
-----

::

    from opencomputer.security.path_safety import (
        UnsafePathError,
        is_safe_path,
        assert_safe_path,
    )

    # boolean check
    if is_safe_path(p, roots=[storage_root]):
        ...

    # raises UnsafePathError on failure (preferred for tool boundaries)
    assert_safe_path(p, roots=[storage_root])
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a path resolves outside every configured safe root."""


def _resolve_strict(path: Path) -> Path | None:
    """Resolve ``path`` to absolute form, returning ``None`` on failure.

    A path that can't be resolved (broken symlink, permission denied,
    bad encoding) is treated as unsafe — there's no way to verify
    containment without a real filesystem path.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def is_safe_path(path: Path | str, *, roots: Iterable[Path | str]) -> bool:
    """Return True iff ``path`` resolves under any of the given roots.

    Parameters
    ----------
    path:
        The path to check. May be absolute, relative, or symlinked.
        Strings are accepted for caller convenience.
    roots:
        Iterable of allowed root directories. Each is resolved before
        comparison so symlinked roots match correctly. An empty
        iterable is allowed (returns False — nothing is safe).

    Notes
    -----
    A NUL byte in the path is treated as unsafe (some kernels truncate
    on NUL, allowing path-confusion).
    """
    if isinstance(path, str):
        if "\0" in path:
            return False
        path = Path(path)
    resolved = _resolve_strict(path)
    if resolved is None:
        return False
    for root in roots:
        if isinstance(root, str):
            root = Path(root)
        resolved_root = _resolve_strict(root)
        if resolved_root is None:
            continue
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            continue
        else:
            return True
    return False


def assert_safe_path(path: Path | str, *, roots: Iterable[Path | str]) -> Path:
    """Like :func:`is_safe_path` but raises :class:`UnsafePathError` on failure.

    Returns the resolved path on success — callers can use the result
    directly without re-resolving.
    """
    candidate = Path(path) if isinstance(path, str) else path
    if not is_safe_path(candidate, roots=roots):
        raise UnsafePathError(
            f"path {str(path)!r} resolves outside the allowed roots"
        )
    resolved = _resolve_strict(candidate)
    assert resolved is not None  # is_safe_path verified this
    return resolved


__all__ = [
    "UnsafePathError",
    "assert_safe_path",
    "is_safe_path",
]
