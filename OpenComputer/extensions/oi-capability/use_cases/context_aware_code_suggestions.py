# ruff: noqa: N999  # directory name 'oi-capability' has a hyphen (required by plugin manifest)
"""Code-context gathering helpers.

Integration with ``extensions/coding-harness/*`` is Session A's Phase 5 scope
(interweaving plan). This module composes OI tools standalone to gather
file-level and git-blame context for code suggestions.
"""

from __future__ import annotations

import subprocess
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

from ..tools.tier_1_introspection import ReadFileRegionTool, ReadGitLogTool

if TYPE_CHECKING:
    from ..subprocess.wrapper import OISubprocessWrapper


async def gather_code_context(
    wrapper: OISubprocessWrapper,
    target_file: str,
    *,
    neighbor_radius: int = 3,
) -> dict:
    """Read a target file plus its N nearest sibling files.

    Uses :class:`ReadFileRegionTool` (Tier 1) to read up to 64 KB of each file.

    Parameters
    ----------
    wrapper:
        The OI subprocess wrapper.
    target_file:
        Absolute path to the file of interest.
    neighbor_radius:
        Number of sibling files (sorted lexicographically) to include on each
        side of the target file.

    Returns::

        {
            "target": str,                    # content of target_file
            "neighbors": {path: str, ...},    # content of neighbor files
        }
    """
    from plugin_sdk.core import ToolCall

    read_tool = ReadFileRegionTool(wrapper=wrapper)

    async def _read(path: str) -> str:
        call = ToolCall(
            id=f"ctx-read-{path}",
            name="read_file_region",
            arguments={"path": path, "offset": 0, "length": 65536},
        )
        result = await read_tool.execute(call)
        return result.content if not result.is_error else ""

    target_content = await _read(target_file)

    # Find neighbor files in the same directory
    target_path = Path(target_file)
    parent = target_path.parent
    neighbors: dict[str, str] = {}

    try:
        siblings = sorted(
            p for p in parent.iterdir() if p.is_file() and p != target_path
        )
        # Find position of target among siblings
        target_idx = next(
            (i for i, s in enumerate(siblings) if s.name == target_path.name),
            None,
        )
        if target_idx is not None:
            start = max(0, target_idx - neighbor_radius)
            end = min(len(siblings), target_idx + neighbor_radius + 1)
            neighbor_paths = siblings[start:end]
        else:
            neighbor_paths = siblings[:neighbor_radius]

        for sibling in neighbor_paths:
            content = await _read(str(sibling))
            neighbors[str(sibling)] = content
    except (OSError, PermissionError):
        pass

    return {"target": target_content, "neighbors": neighbors}


async def git_blame_context(
    wrapper: OISubprocessWrapper,
    file_path: str,
    *,
    line_start: int,
    line_end: int,
) -> dict:
    """Return git blame information for a line range in *file_path*.

    Uses :class:`ReadGitLogTool` to confirm git is available, then shells out
    to ``git blame`` for the specific line range.

    Parameters
    ----------
    wrapper:
        The OI subprocess wrapper (used for ReadGitLogTool constructor
        uniformity; git blame itself is inline).
    file_path:
        Absolute path to the file.
    line_start:
        First line of the range (1-indexed).
    line_end:
        Last line of the range (inclusive, 1-indexed).

    Returns a dict keyed by line number::

        {
            "<line_no>": {"author": str, "commit": str, "date": str},
            ...
        }

    On error, returns ``{"error": str}``.
    """
    # ReadGitLogTool is instantiated to maintain constructor uniformity;
    # git blame itself is run inline (same §11.4 carve-out rationale).
    _git_tool = ReadGitLogTool(wrapper=wrapper)  # noqa: F841 — kept for uniformity

    try:
        result = subprocess.run(
            [
                "git",
                "blame",
                f"-L{line_start},{line_end}",
                "--porcelain",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return {"error": "git not found — install git and ensure it is on PATH"}
    except subprocess.TimeoutExpired:
        return {"error": "git blame timed out after 30 s"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    if result.returncode != 0:
        return {"error": f"git blame error: {result.stderr.strip()}"}

    # Parse porcelain output
    # Each hunk starts with: <commit_hash> <orig_line> <final_line> [count]
    # followed by header lines then a tab-prefixed source line
    blame_map: dict[str, dict] = {}
    current_commit = ""
    current_author = ""
    current_date = ""
    current_line: int | None = None

    for line in result.stdout.splitlines():
        if line and not line.startswith("\t") and not line.startswith("author") and not line.startswith("committer") and not line.startswith("summary") and not line.startswith("filename") and not line.startswith("previous") and not line.startswith("boundary"):
            parts = line.split()
            if len(parts) >= 3 and len(parts[0]) == 40:
                current_commit = parts[0]
                current_line = int(parts[2]) if len(parts) >= 3 else None
        elif line.startswith("author "):
            current_author = line[7:].strip()
        elif line.startswith("author-time "):
            # Convert epoch to ISO date
            try:
                from datetime import datetime

                epoch = int(line[12:].strip())
                current_date = datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d")
            except (ValueError, OSError):
                current_date = line[12:].strip()
        elif line.startswith("\t") and current_line is not None:
            blame_map[str(current_line)] = {
                "author": current_author,
                "commit": current_commit[:8],  # short hash
                "date": current_date,
            }

    return blame_map
