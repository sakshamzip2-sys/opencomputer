"""Render unified diff for Edit / MultiEdit tool results.

Caps the diff at MAX_DIFF_LINES — beyond that, truncates with a count.
Token-cost aware: 500 lines x ~50 chars = 25KB max in the tool result.
"""
from __future__ import annotations

import difflib

MAX_DIFF_LINES = 500


def render_unified_diff(*, before: str, after: str, file_path: str) -> str:
    """Return a truncated unified diff. Empty string if before == after."""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        before_lines, after_lines,
        fromfile=f"{file_path} (before)",
        tofile=f"{file_path} (after)",
        n=3,
    ))
    if len(diff) > MAX_DIFF_LINES:
        omitted = len(diff) - MAX_DIFF_LINES
        diff = diff[:MAX_DIFF_LINES] + [f"... ({omitted} more lines truncated)\n"]
    return "".join(diff)
