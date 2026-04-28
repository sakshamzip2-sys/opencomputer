"""``@filepath`` autocomplete — Hermes Tier 2.B port.

Mirrors ``hermes_cli/commands.py:834-1180`` (the SlashCommandCompleter
project-file completion path) but adapted as a small standalone module
so prompt_toolkit integration in :mod:`input_loop` is the only caller.

Public surface:

- :func:`find_project_files` — enumerate project files (``rg --files``
  → ``fd`` → ``os.walk``) with a 5-second cache + 5000-file cap
- :func:`score_path` — fuzzy score for a query against a path
- :func:`top_matches` — sort files by score, return up to N best
- :func:`extract_at_token` — given input buffer text + cursor position,
  return the ``@<query>`` token if any (else ``None``)
- :func:`format_size_label` — human-readable bytes label for the
  dropdown's secondary line

The dropdown rendering itself is in :mod:`input_loop`. This module only
does the data-side work so it's pytest-friendly without prompt_toolkit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

# Cap to keep the dropdown responsive in monorepos. Anything beyond this
# is filtered out by the rg/fd command (sortr=modified takes the most
# recently touched first, which is what's most likely typed about).
_MAX_FILES = 5000

# Cache TTL — short enough that newly-created files appear quickly,
# long enough to not re-walk on every keystroke.
_CACHE_TTL_S = 5.0

# rg subprocess timeout — never block the prompt for a slow walk.
_WALK_TIMEOUT_S = 2.0

# Module-level cache: (cwd, file_list, captured_at_monotonic).
_CACHE: dict[Path, tuple[list[Path], float]] = {}


def find_project_files(cwd: Path | None = None) -> list[Path]:
    """Enumerate files under ``cwd`` (default: current dir).

    Tries ripgrep first (respects ``.gitignore``), falls back to ``fd``,
    then ``os.walk`` with a hardcoded ignore list. Results are cached
    per-cwd for ``_CACHE_TTL_S`` seconds.

    Returns paths relative to ``cwd``, sorted with most-recently-modified
    first when the underlying tool supports it.
    """
    root = (cwd or Path.cwd()).resolve()
    now = time.monotonic()

    cached = _CACHE.get(root)
    if cached is not None and (now - cached[1]) < _CACHE_TTL_S:
        return cached[0]

    files = (
        _walk_with_rg(root)
        or _walk_with_fd(root)
        or _walk_with_oswalk(root)
    )
    _CACHE[root] = (files, now)
    return files


def _walk_with_rg(root: Path) -> list[Path]:
    """Run ``rg --files --sortr=modified --max-count=<MAX>``."""
    if not shutil.which("rg"):
        return []
    try:
        out = subprocess.run(
            ["rg", "--files", "--sortr=modified"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=_WALK_TIMEOUT_S,
        )
        if out.returncode not in (0, 1):  # 0=ok, 1=no matches
            return []
        lines = out.stdout.splitlines()
        return [Path(L) for L in lines[:_MAX_FILES] if L]
    except (subprocess.TimeoutExpired, OSError):
        return []


def _walk_with_fd(root: Path) -> list[Path]:
    """Fall back to ``fd --type f --hidden --no-ignore-vcs``."""
    fd = shutil.which("fd") or shutil.which("fdfind")
    if not fd:
        return []
    try:
        out = subprocess.run(
            [fd, "--type", "f", "--max-results", str(_MAX_FILES)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=_WALK_TIMEOUT_S,
        )
        if out.returncode != 0:
            return []
        return [Path(L) for L in out.stdout.splitlines() if L]
    except (subprocess.TimeoutExpired, OSError):
        return []


def _walk_with_oswalk(root: Path) -> list[Path]:
    """Pure-Python fallback. Skips well-known noisy dirs."""
    skip_dirs = {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        "dist",
        "build",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Filter dirnames in place so os.walk skips them.
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            out.append(Path(os.path.relpath(os.path.join(dirpath, fn), root)))
            if len(out) >= _MAX_FILES:
                return out
    return out


def score_path(query: str, path: Path) -> int:
    """Fuzzy-rank a path against a query string.

    Higher = better match. Scoring (mirrors Hermes' SlashCommandCompleter):

    - exact filename match: 100
    - path starts with query: 80
    - path substring match: 60
    - path-segment substring: 40
    - fuzzy initials with word boundaries: 25-35
    - no match: 0

    Empty query returns 0 (caller should treat empty query as "show all,
    sorted by mtime" — that's :func:`top_matches`'s job, not this).
    """
    if not query:
        return 0
    q = query.lower()
    p_str = str(path).lower()
    name = path.name.lower()

    if name == q:
        return 100
    if p_str.startswith(q):
        return 80
    if q in name:
        return 70
    if q in p_str:
        return 60
    # Path-segment (between separators) substring match
    parts = p_str.replace("\\", "/").split("/")
    if any(q in seg for seg in parts):
        return 40
    # Fuzzy initials: query chars appear in order, with word-boundary boost
    score = _fuzzy_initials_score(q, p_str)
    return score


def _fuzzy_initials_score(query: str, path: str) -> int:
    """Walk query chars left-to-right; match each in order against path.

    Award 35 if every char appears at a word boundary, 25 if they appear
    in order but mid-word. Zero if any char doesn't appear in order.
    """
    qi = 0
    word_boundary_hits = 0
    boundaries = " /-_."
    last_was_boundary = True  # treat string start as boundary

    for ch in path:
        if qi >= len(query):
            break
        if ch == query[qi]:
            if last_was_boundary:
                word_boundary_hits += 1
            qi += 1
        last_was_boundary = ch in boundaries

    if qi < len(query):
        return 0  # didn't consume all query chars
    if word_boundary_hits == len(query):
        return 35
    return 25


def top_matches(query: str, files: list[Path], n: int = 10) -> list[Path]:
    """Return up to ``n`` best matches for ``query`` against ``files``.

    Empty query returns the first ``n`` files in input order (caller
    should sort by mtime first if that ordering is desired).
    """
    if not query:
        return files[:n]
    scored = [(score_path(query, p), p) for p in files]
    scored = [(s, p) for s, p in scored if s > 0]
    scored.sort(key=lambda x: (-x[0], str(x[1])))
    return [p for _, p in scored[:n]]


def extract_at_token(text: str, cursor_pos: int) -> tuple[str, int, int] | None:
    """If the cursor sits inside an ``@<query>`` token, return ``(query,
    start, end)``. Else ``None``.

    The ``@`` itself must be preceded by start-of-string or whitespace
    (so paths like ``user@host`` don't match). The token ends at the
    next whitespace or string-end.
    """
    if cursor_pos > len(text):
        return None
    # Walk leftward from cursor to find the @ that starts the token.
    i = cursor_pos
    while i > 0 and not text[i - 1].isspace():
        i -= 1
    if i >= len(text) or text[i] != "@":
        return None
    # Ensure char before @ is whitespace or start-of-string.
    if i > 0 and not text[i - 1].isspace():
        return None
    # Walk rightward to end-of-token.
    j = i + 1
    while j < len(text) and not text[j].isspace():
        j += 1
    return (text[i + 1 : j], i, j)


def format_size_label(path: Path, *, base: Path | None = None) -> str:
    """Return a short ``"NN KB"`` / ``"NN MB"`` / ``"dir"`` label."""
    full = (base or Path.cwd()) / path
    try:
        if full.is_dir():
            return "dir"
        size = full.stat().st_size
    except OSError:
        return ""
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size // 1024}KB"
    return f"{size // (1024 * 1024)}MB"


def clear_cache() -> None:
    """Test-helper: drop the per-cwd cache."""
    _CACHE.clear()


__all__ = [
    "clear_cache",
    "extract_at_token",
    "find_project_files",
    "format_size_label",
    "score_path",
    "top_matches",
]
