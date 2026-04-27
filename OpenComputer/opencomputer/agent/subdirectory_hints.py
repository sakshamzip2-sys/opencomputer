"""Progressive subdirectory hint discovery (TS-T5).

As the agent navigates into subdirectories via tool calls (Read, Bash,
Grep, etc.), this module discovers and loads project context files
(``OPENCOMPUTER.md``, ``AGENTS.md``, ``CLAUDE.md``, ``.cursorrules``)
from those directories.  Discovered hints are appended to the tool
result so the model gets relevant context at the moment it starts
working in a new area of the codebase.

This complements the startup context loading in
``prompt_builder.load_workspace_context`` which only loads from the cwd
and its ancestors.  Subdirectory hints are discovered lazily and injected
into the conversation **without modifying the system prompt** — that's
critical because mutating the prompt would invalidate Anthropic's prefix
cache. Appending into a fresh tool-result message keeps the cached
prefix intact.

Inspired by Block/goose's SubdirectoryHintTracker; near-verbatim port of
``hermes-agent-2026.4.23/agent/subdirectory_hints.py`` adapted for OC.
"""

from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _scan_context_content(content: str, _filename: str) -> str:
    """No-op security scan placeholder.

    Hermes runs prompt-injection scanning on workspace context content;
    OC's V3.B MVP does not yet ship that scanner. Defining a no-op here
    preserves the call site so a future security pass can swap in a real
    implementation without touching this module's logic.
    """
    return content


# Context files to look for in subdirectories, in priority order.
# OPENCOMPUTER.md takes precedence (V3.A-T8 convention) — same precedence
# as ``prompt_builder.load_workspace_context``. Within a single
# directory, the FIRST match wins (matching Hermes' "first-match" rule).
_HINT_FILENAMES = [
    "OPENCOMPUTER.md", "opencomputer.md",
    "AGENTS.md", "agents.md",
    "CLAUDE.md", "claude.md",
    ".cursorrules",
]

# Maximum chars per hint file to prevent context bloat. Hermes uses 8000;
# we keep the same value so behavior matches one-for-one.
_MAX_HINT_CHARS = 8_000

# Tool argument keys that typically contain file paths.
_PATH_ARG_KEYS = {"path", "file_path", "workdir"}

# Tools that take shell commands where we should extract paths from
# command tokens. OC's primary shell tool is ``Bash``; ``terminal`` is
# included for parity with Hermes / future plugin tools.
_COMMAND_TOOLS = {"Bash", "terminal"}

# How many parent directories to walk up when looking for hints.
# Prevents scanning all the way to / for deeply nested paths.
_MAX_ANCESTOR_WALK = 5


class SubdirectoryHintTracker:
    """Track which directories the agent visits and load hints on first access.

    Usage::

        tracker = SubdirectoryHintTracker(working_dir="/path/to/project")

        # After each tool call:
        hints = tracker.check_tool_call("Read", {"file_path": "backend/src/main.py"})
        if hints:
            tool_result_content += hints  # append to the tool result string
    """

    def __init__(self, working_dir: str | None = None):
        self.working_dir = Path(working_dir or os.getcwd()).resolve()
        self._loaded_dirs: set[Path] = set()
        # Pre-mark the working dir as loaded — prompt_builder.load_workspace_context
        # already loaded its hints into the system prompt at startup.
        self._loaded_dirs.add(self.working_dir)

    def check_tool_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> str | None:
        """Check tool call arguments for new directories and load any hint files.

        Returns formatted hint text to append to the tool result, or None
        when no new directories were touched / no hint files were found.
        """
        dirs = self._extract_directories(tool_name, tool_args)
        if not dirs:
            return None

        all_hints = []
        for d in dirs:
            hints = self._load_hints_for_directory(d)
            if hints:
                all_hints.append(hints)

        if not all_hints:
            return None

        return "\n\n" + "\n\n".join(all_hints)

    def _extract_directories(
        self, tool_name: str, args: dict[str, Any]
    ) -> list:
        """Extract directory paths from tool call arguments."""
        candidates: set[Path] = set()

        # Direct path arguments
        for key in _PATH_ARG_KEYS:
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                self._add_path_candidate(val, candidates)

        # Shell commands — extract path-like tokens
        if tool_name in _COMMAND_TOOLS:
            cmd = args.get("command", "")
            if isinstance(cmd, str):
                self._extract_paths_from_command(cmd, candidates)

        return list(candidates)

    def _add_path_candidate(self, raw_path: str, candidates: set[Path]):
        """Resolve a raw path and add its directory + ancestors to candidates.

        Walks up from the resolved directory toward the filesystem root,
        stopping at the first directory already in ``_loaded_dirs`` (or after
        ``_MAX_ANCESTOR_WALK`` levels).  This ensures that reading
        ``project/src/main.py`` discovers ``project/AGENTS.md`` even when
        ``project/src/`` has no hint files of its own.
        """
        try:
            p = Path(raw_path).expanduser()
            if not p.is_absolute():
                p = self.working_dir / p
            p = p.resolve()
            # Use parent if it's a file path (has extension or doesn't exist as dir)
            if p.suffix or (p.exists() and p.is_file()):
                p = p.parent
            # Walk up ancestors — stop at already-loaded or root
            for _ in range(_MAX_ANCESTOR_WALK):
                if p in self._loaded_dirs:
                    break
                if self._is_valid_subdir(p):
                    candidates.add(p)
                parent = p.parent
                if parent == p:
                    break  # filesystem root
                p = parent
        except (OSError, ValueError):
            pass

    def _extract_paths_from_command(self, cmd: str, candidates: set[Path]):
        """Extract path-like tokens from a shell command string."""
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            tokens = cmd.split()

        for token in tokens:
            # Skip flags
            if token.startswith("-"):
                continue
            # Must look like a path (contains / or .)
            if "/" not in token and "." not in token:
                continue
            # Skip URLs
            if token.startswith(("http://", "https://", "git@")):
                continue
            self._add_path_candidate(token, candidates)

    def _is_valid_subdir(self, path: Path) -> bool:
        """Check if path is a valid directory to scan for hints."""
        try:
            if not path.is_dir():
                return False
        except OSError:
            return False
        return path not in self._loaded_dirs

    def _load_hints_for_directory(self, directory: Path) -> str | None:
        """Load hint files from a directory. Returns formatted text or None."""
        self._loaded_dirs.add(directory)

        found_hints = []
        for filename in _HINT_FILENAMES:
            hint_path = directory / filename
            try:
                if not hint_path.is_file():
                    continue
            except OSError:
                continue
            try:
                content = hint_path.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                # Same security scan hook as startup context loading (no-op for now).
                content = _scan_context_content(content, filename)
                if len(content) > _MAX_HINT_CHARS:
                    content = (
                        content[:_MAX_HINT_CHARS]
                        + f"\n\n[...truncated {filename}: {len(content):,} chars total]"
                    )
                # Best-effort relative path for display
                rel_path = str(hint_path)
                try:
                    rel_path = str(hint_path.relative_to(self.working_dir))
                except ValueError:
                    try:
                        rel_path = str(hint_path.relative_to(Path.home()))
                        rel_path = "~/" + rel_path
                    except ValueError:
                        pass  # keep absolute
                found_hints.append((rel_path, content))
                # First match wins per directory (like startup loading)
                break
            except Exception as exc:
                logger.debug("Could not read %s: %s", hint_path, exc)

        if not found_hints:
            return None

        sections = []
        for rel_path, content in found_hints:
            sections.append(
                f"[Subdirectory context discovered: {rel_path}]\n{content}"
            )

        logger.debug(
            "Loaded subdirectory hints from %s: %s",
            directory,
            [h[0] for h in found_hints],
        )
        return "\n\n".join(sections)
