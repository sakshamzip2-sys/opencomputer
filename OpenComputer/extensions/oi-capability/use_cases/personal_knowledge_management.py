# ruff: noqa: N999  # directory name 'oi-capability' has a hyphen (required by plugin manifest)
"""Personal Knowledge Management (PKM) helper.

Composes Tier 1 introspection tools to index notes, search across them, and
extract action items from Markdown / Org-mode / plain-text files.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..tools.tier_1_introspection import ListRecentFilesTool, ReadFileRegionTool, SearchFilesTool

if TYPE_CHECKING:
    from ..subprocess.wrapper import OISubprocessWrapper

# File extensions considered as "notes"
_NOTE_EXTENSIONS = frozenset({".md", ".txt", ".org"})

# Regex patterns for action-item extraction
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[ \]\s+(.+)$", re.MULTILINE)
_TODO_RE = re.compile(r"TODO[:\s]+(.+)", re.IGNORECASE)


async def index_recent_notes(
    wrapper: OISubprocessWrapper,
    *,
    days_back: int = 30,
    paths: list[str] | None = None,
) -> dict:
    """Find recently-modified note files (.md / .txt / .org).

    Uses :class:`ListRecentFilesTool` to list files modified within *days_back*
    days, then filters to note extensions.

    Parameters
    ----------
    wrapper:
        The OI subprocess wrapper.
    days_back:
        Look-back window in days (converted to hours for the underlying tool).
    paths:
        Optional list of directories to search. Defaults to the home directory.

    Returns::

        {
            "notes": ["/path/to/note.md", ...],
            "count": int,
            "extensions_found": {"md": int, "txt": int, "org": int},
        }
    """
    from plugin_sdk.core import ToolCall

    tool = ListRecentFilesTool(wrapper=wrapper)
    directories = paths if paths else ["~"]

    all_files: list[str] = []
    for directory in directories:
        call = ToolCall(
            id=f"list-recent-files-{directory}",
            name="list_recent_files",
            arguments={"hours": days_back * 24, "directory": directory, "limit": 200},
        )
        result = await tool.execute(call)
        if result.is_error or not result.content.strip():
            continue
        # Parse file list from output (one path per line, possibly with ls -lt prefixes)
        for line in result.content.splitlines():
            # ls -lt output has the path last; plain find output has the path only
            parts = line.strip().split()
            if parts:
                file_path = parts[-1]
                all_files.append(file_path)

    # Filter by extension
    note_files: list[str] = []
    ext_counts: dict[str, int] = {"md": 0, "txt": 0, "org": 0}
    for fp in all_files:
        for ext in _NOTE_EXTENSIONS:
            if fp.lower().endswith(ext):
                note_files.append(fp)
                ext_counts[ext.lstrip(".")] = ext_counts.get(ext.lstrip("."), 0) + 1
                break

    return {
        "notes": note_files,
        "count": len(note_files),
        "extensions_found": ext_counts,
    }


async def search_notes(wrapper: OISubprocessWrapper, query: str) -> list[dict]:
    """Search for notes matching *query*.

    Delegates to :class:`SearchFilesTool` (Tier 1 / aifs-backed).

    Returns a list of::

        {"path": "/abs/path/file.md", "snippet": "<raw match context>"}
    """
    from plugin_sdk.core import ToolCall

    tool = SearchFilesTool(wrapper=wrapper)
    call = ToolCall(
        id="search-notes",
        name="search_files",
        arguments={"query": query},
    )
    result = await tool.execute(call)

    if result.is_error or not result.content.strip():
        return []

    raw = result.content.strip()
    hits: list[dict] = []
    try:
        import ast

        parsed = ast.literal_eval(raw)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    hits.append(item)
                else:
                    hits.append({"path": str(item), "snippet": ""})
        elif isinstance(parsed, dict):
            hits.append(parsed)
    except (ValueError, SyntaxError):
        for line in raw.splitlines():
            line = line.strip()
            if line:
                hits.append({"path": line, "snippet": ""})

    return hits


async def extract_action_items(wrapper: OISubprocessWrapper, file_path: str) -> list[str]:
    """Extract unchecked checkboxes and inline TODOs from *file_path*.

    Uses :class:`ReadFileRegionTool` to read up to 64 KB of the file, then
    applies two regex patterns:

    * ``r"^\\s*[-*]\\s*\\[ \\]\\s+(.+)$"``  — unchecked Markdown/Org checkboxes
    * ``r"TODO[:\\s]+(.+)"``                  — inline TODO comments

    Returns a deduplicated list of action-item strings.
    """
    from plugin_sdk.core import ToolCall

    tool = ReadFileRegionTool(wrapper=wrapper)
    call = ToolCall(
        id=f"extract-actions-{file_path}",
        name="read_file_region",
        arguments={"path": file_path, "offset": 0, "length": 65536},
    )
    result = await tool.execute(call)

    if result.is_error or not result.content.strip():
        return []

    content = result.content
    seen: set[str] = set()
    items: list[str] = []

    for match in _CHECKBOX_RE.finditer(content):
        text = match.group(1).strip()
        if text and text not in seen:
            seen.add(text)
            items.append(text)

    for match in _TODO_RE.finditer(content):
        text = match.group(1).strip()
        if text and text not in seen:
            seen.add(text)
            items.append(text)

    return items
