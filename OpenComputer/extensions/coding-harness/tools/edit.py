"""Edit tool — surgical find/replace with uniqueness check (Claude Code shape)."""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

# V3.A-T5: every error path is engineered to TEACH the model how to recover —
# the message points to the next concrete action (Read, Glob, replace_all,
# Write, etc.) rather than just describing what went wrong. The
# ``opencomputer.tools._file_read_state`` module is a process-local set of
# paths that have been Read (or Written), so we can enforce the
# "Read first" contract that Edit's description has always promised.
from opencomputer.tools._file_read_state import has_been_read, mark_read
from opencomputer.tools.edit_diff_format import render_unified_diff


class EditTool(BaseTool):
    parallel_safe = False  # writes to disk

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Edit",
            description=(
                "Surgical find-and-replace in an existing file. The preferred way to "
                "modify code: cheaper than Write (only sends the diff), preserves "
                "everything you don't touch, and uniqueness check catches accidental "
                "double-replacements.\n\n"
                "- READ FIRST: you must Read the file at least once in this conversation "
                "before editing — the harness tracks state and will refuse otherwise. "
                "When matching against Read's output, strip the line-number prefix "
                "(format is `<num>\\t<content>`); never include the line numbers in "
                "old_string.\n"
                "- `old_string` must match the file content EXACTLY, including all "
                "whitespace and indentation.\n"
                "- `old_string` must be UNIQUE in the file. If it appears multiple times "
                "the tool errors — expand old_string with more surrounding context until "
                "unique, or pass `replace_all: true`.\n"
                "- Don't use Edit for new files; use Write. Don't use it on .ipynb "
                "notebooks; use NotebookEdit.\n"
                "- Prefer Edit over Bash sed/awk: structured errors and the harness can "
                "track the change for rewind/diff."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to edit.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact text to replace (must be unique unless replace_all).",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The replacement text.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace all occurrences (default false).",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        path = Path(args.get("file_path", ""))
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        replace_all = bool(args.get("replace_all", False))

        if not path.is_absolute():
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"file_path must be an absolute path, got: {path!s}. "
                    f"Edit requires absolute paths to avoid ambiguity. "
                    f"Tip: prefix with the project root (e.g. /Users/you/project/...)."
                ),
                is_error=True,
            )
        if not path.exists():
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"{path} does not exist. Edit only modifies existing files. "
                    f"If you meant to create a new file, use the Write tool. "
                    f"If you're unsure where the file lives, use Glob "
                    f"(e.g. pattern='**/{path.name}') to locate it."
                ),
                is_error=True,
            )
        if path.is_dir():
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"{path} is a directory, not a file. Edit operates on files only. "
                    f"Use Glob to list directory contents (e.g. pattern='{path}/**'), "
                    f"or Read on a specific file inside it."
                ),
                is_error=True,
            )
        if not path.is_file():
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"{path} is not a regular file (it may be a socket, fifo, or "
                    f"device). Edit can only modify regular files."
                ),
                is_error=True,
            )
        if old == new:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "old_string and new_string are identical — Edit would be a no-op "
                    "and is rejected to surface the mistake. "
                    "If you wanted to inspect the file, use Read. "
                    "If you wanted to verify a change, the previous Edit's success "
                    "message already confirmed it landed."
                ),
                is_error=True,
            )

        if not has_been_read(path):
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"You must Read {path} before editing it. "
                    f"Edit relies on the file's known state from a prior Read in "
                    f"this conversation — without it, your old_string is a guess "
                    f"and likely to mismatch. Call Read on this path first, then "
                    f"retry Edit with old_string copied byte-for-byte from Read's "
                    f"output (after stripping the line-number prefix)."
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
                    f"or it was deleted between your Read and this Edit. "
                    f"Re-Read the file and retry."
                ),
                is_error=True,
            )

        count = text.count(old)
        if count == 0:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"old_string was not found in {path}. The old_string must match "
                    f"the file's CURRENT content byte-for-byte, including indentation, "
                    f"trailing whitespace, and line endings. "
                    f"Common causes: (1) the file changed since you last Read it — "
                    f"Read it again to get fresh bytes; (2) you copied old_string from "
                    f"Read's output without stripping the line-number prefix "
                    f"(`<num>\\t<content>`); (3) your old_string spans a tab/space "
                    f"mismatch; (4) Windows vs. Unix line endings differ. "
                    f"Tip: Read the file again and copy the exact bytes you want to replace."
                ),
                is_error=True,
            )
        if count > 1 and not replace_all:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"old_string appears {count} times in {path}, so Edit can't tell "
                    f"which occurrence you mean. Either: "
                    f"(1) provide more surrounding context (a few extra lines before "
                    f"and/or after) to make old_string unique, or "
                    f"(2) pass replace_all=true to replace every occurrence at once. "
                    f"If the duplicates are in different functions, expanding the "
                    f"old_string to include the surrounding `def`/signature line is "
                    f"usually the cleanest fix."
                ),
                is_error=True,
            )

        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        try:
            path.write_text(new_text, encoding="utf-8")
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error writing {path}: {type(e).__name__}: {e}. "
                    f"Likely causes: insufficient permissions, full disk, or the file "
                    f"is locked by another process. Check filesystem state and retry."
                ),
                is_error=True,
            )

        # The bytes we just wrote are by definition the file's current state,
        # so subsequent Edits in this turn don't need a fresh Read.
        mark_read(path)

        n = count if replace_all else 1
        # V3.A-T6: include a unified diff in the success message so the model
        # can verify what it changed without a follow-up Read. The diff is
        # capped at MAX_DIFF_LINES (500) to keep token cost bounded.
        diff = render_unified_diff(before=text, after=new_text, file_path=str(path))
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"Edited {path} ({n} replacement{'s' if n != 1 else ''})\n\n"
                f"Diff:\n{diff}"
            ),
        )


__all__ = ["EditTool"]
