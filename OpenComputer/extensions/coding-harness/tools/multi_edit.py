"""MultiEdit tool — atomic batch edits to a single file."""

from __future__ import annotations

from pathlib import Path

# V3.A-T5: per-edit failures must teach the model how to recover. Each error
# message identifies WHICH edit in the batch failed (1-indexed for human
# readability), what the underlying problem is, and the next concrete action
# to take. The shared read-state tracker in opencomputer.tools enforces the
# same "Read first" contract MultiEdit's description has always promised.
from opencomputer.tools._file_read_state import has_been_read, mark_read
from opencomputer.tools.edit_diff_format import render_unified_diff
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class MultiEditTool(BaseTool):
    parallel_safe = False

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="MultiEdit",
            description=(
                "Apply multiple Edit operations to a SINGLE file atomically. All edits "
                "are applied in-memory in order; the file is written exactly once at "
                "the end. If ANY edit fails (missing text, non-unique without "
                "replace_all, no-op pair), the whole batch is rolled back and the file "
                "stays unchanged. Use this when you need to make 3+ changes to the same "
                "file — it's more efficient than calling Edit repeatedly and gives you "
                "all-or-nothing semantics. CAUTION: edits run sequentially, so a later "
                "edit's `old_string` must match what earlier edits leave behind. Read "
                "first (same harness rule as Edit). For NEW files use Write; for changes "
                "across multiple files, dispatch one MultiEdit per file (preferably in "
                "parallel where independent). Prefer MultiEdit + review over repeatedly "
                "Writing the same file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to edit.",
                    },
                    "edits": {
                        "type": "array",
                        "description": "List of edits to apply in order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {"type": "string"},
                                "new_string": {"type": "string"},
                                "replace_all": {"type": "boolean"},
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                },
                "required": ["file_path", "edits"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        path = Path(args.get("file_path", ""))
        edits = args.get("edits", [])

        if not path.is_absolute():
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"file_path must be an absolute path, got: {path!s}. "
                    f"MultiEdit requires absolute paths to avoid ambiguity. "
                    f"Tip: prefix with the project root."
                ),
                is_error=True,
            )
        if not path.exists():
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"{path} does not exist. MultiEdit only modifies existing files. "
                    f"If you meant to create a new file, use Write. "
                    f"If you're unsure where the file lives, use Glob to locate it."
                ),
                is_error=True,
            )
        if path.is_dir():
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"{path} is a directory, not a file. MultiEdit operates on files only. "
                    f"Use Glob (pattern='{path}/**') to list directory contents."
                ),
                is_error=True,
            )
        if not path.is_file():
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"{path} is not a regular file. MultiEdit can only modify regular files."
                ),
                is_error=True,
            )
        if not isinstance(edits, list) or not edits:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "edits must be a non-empty list. Provide at least one "
                    "{old_string, new_string} object. If you only need to change one "
                    "thing, prefer the Edit tool — it has a simpler shape."
                ),
                is_error=True,
            )

        if not has_been_read(path):
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"You must Read {path} before editing it. "
                    f"MultiEdit relies on the file's known state from a prior Read in "
                    f"this conversation — without it, your old_strings are guesses "
                    f"and likely to mismatch. Call Read on this path first, then "
                    f"retry MultiEdit with each old_string copied byte-for-byte from "
                    f"Read's output (after stripping the line-number prefix)."
                ),
                is_error=True,
            )

        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error reading {path}: {type(e).__name__}: {e}. "
                    f"Likely causes: file became unreadable, encoding is not UTF-8, "
                    f"or it was deleted between your Read and this MultiEdit. "
                    f"Re-Read the file and retry."
                ),
                is_error=True,
            )

        original = text
        total_replacements = 0

        for i, edit in enumerate(edits):
            # Show the model a 1-indexed position so it can match the error
            # message against its own input order ("edit 1 of 3" not "edit 0").
            edit_label = f"edit #{i + 1} of {len(edits)}"
            old = edit.get("old_string", "")
            new = edit.get("new_string", "")
            replace_all = bool(edit.get("replace_all", False))

            if old == new:
                return ToolResult(
                    tool_call_id=call.id,
                    content=(
                        f"{edit_label} failed: old_string and new_string are identical — "
                        f"Edit would be a no-op. The whole batch was rolled back; the "
                        f"file is unchanged. Remove the no-op edit and resubmit. "
                        f"If you wanted to inspect the file, use Read instead."
                    ),
                    is_error=True,
                )

            count = text.count(old)
            if count == 0:
                return ToolResult(
                    tool_call_id=call.id,
                    content=(
                        f"{edit_label} failed: old_string was not found in the "
                        f"in-memory file state. The whole batch was rolled back; "
                        f"{total_replacements} prior in-memory edits were discarded "
                        f"and {path} is unchanged on disk. "
                        f"Remember: edits apply sequentially, so a later edit must "
                        f"match what earlier edits in this same batch produced. "
                        f"If your old_string was correct against the original file, "
                        f"check whether an earlier edit in this batch already replaced "
                        f"those bytes. Otherwise: Read the file again and copy "
                        f"old_string byte-for-byte (matching whitespace and line endings) "
                        f"from the fresh Read output."
                    ),
                    is_error=True,
                )
            if count > 1 and not replace_all:
                return ToolResult(
                    tool_call_id=call.id,
                    content=(
                        f"{edit_label} failed: old_string appears {count} times in the "
                        f"in-memory file state. The whole batch was rolled back; "
                        f"{total_replacements} prior in-memory edits were discarded "
                        f"and {path} is unchanged on disk. "
                        f"Either: (1) expand old_string with more surrounding context "
                        f"(a few extra lines) to make it unique, or "
                        f"(2) set replace_all=true on this edit to replace every "
                        f"occurrence."
                    ),
                    is_error=True,
                )

            text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            total_replacements += count if replace_all else 1

        # All edits succeeded in memory. Write once.
        if text == original:
            # Possible for a no-op batch (e.g. old and new match after prior edits)
            return ToolResult(
                tool_call_id=call.id, content="All edits produced no net change"
            )
        try:
            path.write_text(text, encoding="utf-8")
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error writing {path}: {type(e).__name__}: {e}. "
                    f"All in-memory edits were prepared successfully but the disk "
                    f"write failed. Likely causes: insufficient permissions, full "
                    f"disk, or the file is locked by another process. Resolve and "
                    f"retry the MultiEdit."
                ),
                is_error=True,
            )

        # Mark the path as Read so subsequent Edits in this turn don't need
        # a fresh Read — we just wrote known bytes.
        mark_read(path)

        # V3.A-T6: render ONE diff for the entire batch — `original` is the
        # file's content before any edits in the batch, `text` is the final
        # in-memory state after all edits applied. Capped at MAX_DIFF_LINES.
        diff = render_unified_diff(before=original, after=text, file_path=str(path))
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"Applied {len(edits)} edit(s) to {path} ({total_replacements} replacements)\n\n"
                f"Diff:\n{diff}"
            ),
        )


__all__ = ["MultiEditTool"]
