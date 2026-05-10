"""Pinned-files mechanism — system-prompt injection of frequently-reread files.

Background
----------

``oc optimize`` (Grade E example):

    1. [HIGH] reread_file
       → opencomputer/agent/loop.py read 10× across 4 sessions
         (~254711 bytes/read)
       → Save: ~573,099 tokens ($1.7193)
       → Fix: Pin to the system prompt or cache via `oc skills add` /
              a CLAUDE.md anchor so subsequent sessions don't re-read it.

This module implements the "Pin to the system prompt" half. Files
listed in ``Config.prompt.pinned_files`` get their contents loaded into
the prompt at session start, so the agent SEES the file content
without ever calling the ``Read`` tool — eliminating per-session
re-read cost.

Surface
-------

* :func:`render_pinned_files_block` — the only thing the prompt builder
  needs. Takes paths + cap, returns the markdown block to inject (or
  empty string when the list is empty / nothing readable). Pure;
  testable without the prompt builder.
* :func:`add_pinned_file` / :func:`remove_pinned_file` — config mutation
  helpers used by the ``oc pin`` / ``oc unpin`` CLI subcommands. Both
  preserve insertion order, deduplicate, and persist via
  :func:`opencomputer.agent.config_store.save_config`.

Behavior
--------

* Files are read fresh each session — edits propagate without an
  explicit "refresh".
* Each file rendered as a fenced code block prefixed with the path,
  using the file extension as the language hint when known.
* Missing or unreadable files are logged at WARNING and skipped (the
  pin doesn't break the session).
* Total size is capped (``max_total_bytes`` from config). Once the cap
  is hit, remaining files are skipped and a single WARNING summarises
  which files were dropped — operator's signal to prune the list.
* Path expansion: ``~`` is expanded to the user's home; relative paths
  are resolved against the current working directory at the moment of
  rendering.
* No security gating beyond what the OS enforces — the user explicitly
  asked to pin a file, so we trust the path. Symlinks are followed.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("opencomputer.agent.pinned_files")


_LANG_HINTS: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".sh": "bash",
    ".zsh": "zsh",
    ".bash": "bash",
    ".rb": "ruby",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".md": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".xml": "xml",
}


def _resolve_path(raw: str) -> Path:
    """Expand ``~`` and resolve relative-to-cwd. Pure; no I/O."""
    return Path(raw).expanduser().resolve()


def _lang_hint(p: Path) -> str:
    """Markdown fence language hint from file extension (lower-case)."""
    return _LANG_HINTS.get(p.suffix.lower(), "")


def render_pinned_files_block(
    paths: tuple[str, ...] | list[str],
    *,
    max_total_bytes: int = 200_000,
) -> str:
    """Build the markdown block to inject into the system prompt.

    Empty paths → empty string (caller treats as "no slot rendered").
    All paths missing / unreadable → empty string + WARNING.

    Returns a string of the form::

        # /abs/path/to/file.py
        ```python
        <file contents>
        ```

        # /abs/path/to/other.md
        ```markdown
        <file contents>
        ```

    Concatenated with blank-line separators. The caller wraps this in
    a ``<pinned-files>...</pinned-files>`` slot inside ``base.j2``.

    The byte cap applies to the COMBINED content of all included files
    (raw bytes, not bytes-in-prompt). Files are processed in the order
    given; once the cap is hit, the remaining files are dropped and a
    single WARNING summarises what was skipped.
    """
    if not paths:
        return ""

    if max_total_bytes <= 0:
        logger.warning(
            "render_pinned_files_block: max_total_bytes=%d ≤ 0; "
            "rendering empty block",
            max_total_bytes,
        )
        return ""

    parts: list[str] = []
    total_bytes = 0
    skipped_too_large: list[str] = []
    skipped_unreadable: list[str] = []

    for raw in paths:
        if not raw or not isinstance(raw, str):
            logger.warning(
                "render_pinned_files_block: ignoring invalid path %r", raw
            )
            continue

        try:
            path = _resolve_path(raw)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "render_pinned_files_block: cannot resolve %r: %s", raw, exc
            )
            skipped_unreadable.append(raw)
            continue

        if not path.exists():
            logger.warning(
                "render_pinned_files_block: pinned file does not exist: %s "
                "(remove with `oc unpin %s`)",
                path,
                raw,
            )
            skipped_unreadable.append(raw)
            continue

        if not path.is_file():
            logger.warning(
                "render_pinned_files_block: pinned path is not a file: %s "
                "(remove with `oc unpin %s`)",
                path,
                raw,
            )
            skipped_unreadable.append(raw)
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning(
                "render_pinned_files_block: read failed for %s: %s "
                "(remove with `oc unpin %s`)",
                path,
                exc,
                raw,
            )
            skipped_unreadable.append(raw)
            continue

        size = len(content.encode("utf-8", errors="replace"))
        if total_bytes + size > max_total_bytes:
            skipped_too_large.append(raw)
            continue

        total_bytes += size
        lang = _lang_hint(path)
        parts.append(
            f"# {path}\n```{lang}\n{content}\n```"
        )

    if skipped_too_large:
        logger.warning(
            "render_pinned_files_block: cap %d bytes reached after %d files; "
            "skipped %d (%s) — prune with `oc unpin <path>` or raise "
            "`prompt.max_total_bytes` in config.yaml",
            max_total_bytes,
            len(parts),
            len(skipped_too_large),
            ", ".join(skipped_too_large),
        )

    if not parts:
        # All paths failed — return empty so the slot doesn't render an
        # empty block. The per-file WARNINGs above already explain why.
        return ""

    return "\n\n".join(parts)


def normalize_pinned_path(raw: str) -> str:
    """Canonical form for storing in config.

    Resolves ``~`` and relative paths to absolute. The stored value
    survives ``cd`` between sessions, which is what the user expects
    when they ran ``oc pin some/file.py`` from a project root.
    """
    return str(_resolve_path(raw))


def add_pinned_file(existing: tuple[str, ...], new_path: str) -> tuple[str, ...]:
    """Return a new tuple with *new_path* appended (deduped, normalized).

    Order-preserving; if the normalized path is already present, returns
    the original tuple unchanged. The caller persists via
    :func:`opencomputer.agent.config_store.save_config`.

    Raises ``FileNotFoundError`` if the path doesn't exist at the time
    of pinning — fails loudly so the user can correct a typo. (This is
    distinct from the rendering path, where a once-valid pin that goes
    missing later is just a WARNING.)
    """
    norm = normalize_pinned_path(new_path)
    if not Path(norm).exists():
        raise FileNotFoundError(
            f"cannot pin {new_path!r} — no such file: {norm}"
        )
    if not Path(norm).is_file():
        raise IsADirectoryError(
            f"cannot pin {new_path!r} — not a regular file: {norm}"
        )
    if norm in existing:
        return existing
    return tuple(existing) + (norm,)


def remove_pinned_file(existing: tuple[str, ...], target: str) -> tuple[str, ...]:
    """Return a new tuple with *target* removed.

    Matches BOTH the raw and the normalized form, so `oc unpin some/path.py`
    works when the stored value is the absolute path. If the target
    isn't present, returns the original tuple unchanged.
    """
    norm = normalize_pinned_path(target)
    return tuple(p for p in existing if p != norm and p != target)


__all__ = [
    "add_pinned_file",
    "normalize_pinned_path",
    "remove_pinned_file",
    "render_pinned_files_block",
]
