"""NotebookEdit tool — insert/replace/delete cells in a Jupyter notebook.

`Read` already handles `.ipynb` files (per its own docstring), so we don't
ship `NotebookRead`. This tool is the *write* counterpart, modelled on
claude-code's `NotebookEdit`.

Notebook format ref: https://nbformat.readthedocs.io/en/latest/format_description.html
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


def _new_cell_id() -> str:
    """nbformat 4.5+ requires unique 8-char-ish cell ids."""
    return uuid.uuid4().hex[:8]


def _make_cell(cell_type: str, source: str) -> dict[str, Any]:
    """Build a minimal nbformat-4-compliant cell dict."""
    if cell_type not in ("code", "markdown", "raw"):
        raise ValueError(f"unknown cell_type {cell_type!r}")
    cell: dict[str, Any] = {
        "cell_type": cell_type,
        "id": _new_cell_id(),
        "metadata": {},
        "source": source,
    }
    if cell_type == "code":
        cell["execution_count"] = None
        cell["outputs"] = []
    return cell


class NotebookEditTool(BaseTool):
    # Not parallel-safe: edits a file in place.
    parallel_safe = False
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="NotebookEdit",
            description=(
                "Edit a Jupyter notebook (.ipynb) by cell — insert, replace, or delete. "
                "Use this when changing notebook structure (adding code/markdown cells, "
                "swapping out a buggy cell). For inserts, `cell_index` is where the new "
                "cell goes (append by passing an index past the end). For replace/delete, "
                "`cell_index` identifies the target. To READ a notebook, use Read — the "
                "Read tool already handles .ipynb parsing. Prefer NotebookEdit over "
                "Edit/Write on .ipynb: notebooks are JSON with strict cell structure, "
                "raw text edits will corrupt them. The tool preserves cell ids and "
                "nbformat-4 invariants automatically."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or cwd-relative path to the .ipynb file.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["insert", "replace", "delete"],
                        "description": "What to do at cell_index.",
                    },
                    "cell_index": {
                        "type": "integer",
                        "description": (
                            "0-based cell index. For insert, may equal len(cells) "
                            "(appends). For replace/delete, must be a valid index."
                        ),
                    },
                    "cell_type": {
                        "type": "string",
                        "enum": ["code", "markdown", "raw"],
                        "description": "Required for insert/replace; ignored for delete.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Cell source. Required for insert/replace.",
                    },
                },
                "required": ["path", "mode", "cell_index"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        raw_path = str(args.get("path", "")).strip()
        mode = str(args.get("mode", "")).strip()
        try:
            cell_index = int(args.get("cell_index"))
        except (TypeError, ValueError):
            return ToolResult(
                tool_call_id=call.id,
                content="Error: cell_index must be an integer",
                is_error=True,
            )

        if not raw_path:
            return ToolResult(
                tool_call_id=call.id, content="Error: path is required", is_error=True
            )
        if mode not in ("insert", "replace", "delete"):
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: mode must be insert | replace | delete (got {mode!r})",
                is_error=True,
            )

        path = Path(raw_path)
        if not path.exists():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: file not found: {path}",
                is_error=True,
            )
        if path.suffix != ".ipynb":
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: path must end in .ipynb (got {path.suffix!r})",
                is_error=True,
            )

        try:
            nb = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: not valid JSON: {e}",
                is_error=True,
            )

        cells = nb.get("cells")
        if not isinstance(cells, list):
            return ToolResult(
                tool_call_id=call.id,
                content="Error: notebook has no 'cells' array",
                is_error=True,
            )

        # Bounds check — insert allows index == len (append).
        if mode == "insert":
            if cell_index < 0 or cell_index > len(cells):
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"Error: cell_index {cell_index} out of bounds (have {len(cells)})",
                    is_error=True,
                )
        else:
            if cell_index < 0 or cell_index >= len(cells):
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"Error: cell_index {cell_index} out of bounds (have {len(cells)})",
                    is_error=True,
                )

        if mode == "delete":
            removed = cells.pop(cell_index)
            description = f"deleted {removed.get('cell_type','?')} cell at {cell_index}"
        else:
            cell_type = str(args.get("cell_type", "")).strip()
            source = str(args.get("source", ""))
            if not cell_type:
                return ToolResult(
                    tool_call_id=call.id,
                    content="Error: cell_type is required for insert/replace",
                    is_error=True,
                )
            try:
                new_cell = _make_cell(cell_type, source)
            except ValueError as e:
                return ToolResult(
                    tool_call_id=call.id, content=f"Error: {e}", is_error=True
                )
            if mode == "insert":
                cells.insert(cell_index, new_cell)
                description = f"inserted {cell_type} cell at {cell_index}"
            else:
                cells[cell_index] = new_cell
                description = f"replaced cell at {cell_index} with {cell_type}"

        path.write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
        return ToolResult(
            tool_call_id=call.id,
            content=f"OK: {description}. Notebook now has {len(cells)} cell(s).",
        )


__all__ = ["NotebookEditTool"]
