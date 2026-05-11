"""Display-side truncation for tool results.

Ported from pi's ``packages/coding-agent/src/core/tools/truncate.ts`` +
``modes/interactive/components/visual-truncate.ts`` (2026-05-11).

Distinct from :mod:`opencomputer.agent.tokenjuice`, which rewrites tool
results *before* they reach the model. Visual-truncate runs at *display*
time only — the model still sees the full result; the chat just shows a
shortened view. Full output remains accessible via ``oc session show``.

Pattern: dual-limit truncation. Either ``max_lines`` or ``max_bytes`` —
whichever caps first wins. Never returns partial lines (except the bash
tail edge case where the final partial line is the one the user wants
most).

Three modes:

* :func:`truncate_head` — keep the first N lines (file reads, where the
  beginning is signal).
* :func:`truncate_tail` — keep the last N lines (bash output, where the
  command's final state is signal).
* :func:`truncate_middle` — keep first + last with an explicit elision
  marker (OC extension over pi; bash output often has useful info on
  both ends — the command at the top, the error at the bottom).
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_LINES: int = 2000
DEFAULT_MAX_BYTES: int = 50 * 1024  # 50 KB


@dataclass(frozen=True, slots=True)
class TruncationResult:
    """Outcome of a truncation pass. Frozen so callers can't accidentally
    mutate state that the renderer relied on for layout."""

    content: str
    truncated: bool
    truncated_by: str | None  # "lines" | "bytes" | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int


def format_size(num_bytes: int) -> str:
    """Render bytes as ``B`` / ``KB`` / ``MB``. Used in the
    ``[N lines omitted]`` elision marker."""
    if num_bytes < 1024:
        return f"{num_bytes}B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f}KB"
    return f"{num_bytes / (1024 * 1024):.1f}MB"


def _count(text: str) -> tuple[list[str], int, int]:
    """Return (lines, total_lines, total_bytes). One source of truth so
    head/tail/middle all agree on the totals."""
    if not text:
        return ([""], 1, 0)
    lines = text.split("\n")
    return (lines, len(lines), len(text.encode("utf-8")))


def truncate_head(
    text: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Keep the first N lines / bytes. Never returns partial lines.

    If even the first line exceeds the byte cap, returns empty content
    with ``first_line_exceeds_limit=True`` so the caller can surface a
    "[line too long]" placeholder instead of garbled half-lines.
    """
    lines, total_lines, total_bytes = _count(text)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=text,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    # Walk lines from the head, accumulating bytes; stop when either cap
    # would be exceeded.
    kept: list[str] = []
    kept_bytes = 0
    truncated_by: str | None = None
    for idx, line in enumerate(lines):
        line_bytes = len(line.encode("utf-8")) + (1 if idx < total_lines - 1 else 0)
        if idx >= max_lines:
            truncated_by = "lines"
            break
        if kept_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        kept.append(line)
        kept_bytes += line_bytes

    if not kept:
        # First line itself was over the byte cap — return empty with a
        # signal so the caller can render a placeholder.
        return TruncationResult(
            content="",
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=0,
            output_bytes=0,
            last_line_partial=False,
            first_line_exceeds_limit=True,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    content = "\n".join(kept)
    return TruncationResult(
        content=content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(kept),
        output_bytes=len(content.encode("utf-8")),
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_tail(
    text: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Keep the last N lines / bytes. Standard bash output mode."""
    lines, total_lines, total_bytes = _count(text)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=text,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    truncated_by: str | None = None
    # Walk lines from the tail.
    kept_reversed: list[str] = []
    kept_bytes = 0
    for idx, line in enumerate(reversed(lines)):
        line_bytes = len(line.encode("utf-8")) + (1 if idx > 0 else 0)
        if idx >= max_lines:
            truncated_by = "lines"
            break
        if kept_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        kept_reversed.append(line)
        kept_bytes += line_bytes

    if not kept_reversed:
        # Even the very last line was over the byte cap. Bash output:
        # showing nothing is useless — emit a partial-tail of the bytes
        # the user is most likely to care about (the latest stderr).
        last = lines[-1]
        partial = last.encode("utf-8")[-max_bytes:].decode("utf-8", errors="replace")
        return TruncationResult(
            content=partial,
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=1,
            output_bytes=len(partial.encode("utf-8")),
            last_line_partial=True,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    kept = list(reversed(kept_reversed))
    content = "\n".join(kept)
    return TruncationResult(
        content=content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(kept),
        output_bytes=len(content.encode("utf-8")),
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_middle(
    text: str,
    *,
    max_lines: int = 40,
) -> TruncationResult:
    """Keep first + last lines with an explicit elision marker.

    OC extension over pi. Bash output often has useful info on *both*
    ends — the command at the top, the error at the bottom — and pure
    tail truncation loses the "what was the command" context.

    Below 4 lines per side there's no useful elision; falls back to
    :func:`truncate_tail` so callers don't need to branch.
    """
    lines, total_lines, total_bytes = _count(text)

    if total_lines <= max_lines:
        return TruncationResult(
            content=text,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=DEFAULT_MAX_BYTES,
        )

    # Below 4 lines split is meaningless — degrade to tail (which is the
    # right call for very tight budgets: keep the most-recent info).
    if max_lines < 4:
        return truncate_tail(text, max_lines=max_lines)

    # Split the budget. Head/tail when odd: head <= tail (errors at the
    # bottom are usually more important than headers).
    tail_n = (max_lines + 1) // 2
    head_n = max_lines - tail_n
    head_lines = lines[:head_n]
    tail_lines = lines[-tail_n:]
    omitted = total_lines - head_n - tail_n

    marker = f"… [{omitted} lines omitted] …"
    rendered = head_lines + [marker] + tail_lines
    content = "\n".join(rendered)

    return TruncationResult(
        content=content,
        truncated=True,
        truncated_by="lines",
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(rendered),
        output_bytes=len(content.encode("utf-8")),
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=DEFAULT_MAX_BYTES,
    )
