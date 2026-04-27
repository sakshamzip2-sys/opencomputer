"""V3.A-T9 — NotebookEdit smoke against a real .ipynb fixture.

Exercises insert / replace / delete cell paths end-to-end against a real
on-disk .ipynb file.  Tests verify that:

  - on-disk state is mutated correctly after each operation
  - error paths (missing file, corrupt JSON, bad mode, out-of-range index,
    missing required args) return ToolResult(is_error=True) rather than
    raising exceptions

Real schema (from opencomputer/tools/notebook_edit.py):
  Required: path (str), mode ("insert"|"replace"|"delete"), cell_index (int)
  For insert/replace: cell_type ("code"|"markdown"|"raw"), source (str)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencomputer.tools.notebook_edit import NotebookEditTool
from plugin_sdk.core import ToolCall

# ─── Fixture helpers ──────────────────────────────────────────────────────────


def _build_notebook(path: Path) -> None:
    """Write a minimal nbformat-4.5 notebook with two cells to *path*."""
    notebook = {
        "cells": [
            {
                "cell_type": "markdown",
                "id": "aa000001",
                "metadata": {},
                "source": ["# Title\n"],
            },
            {
                "cell_type": "code",
                "id": "aa000002",
                "metadata": {},
                "execution_count": None,
                "outputs": [],
                "source": ["x = 1\n", "print(x)"],
            },
        ],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.13.0"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    path.write_text(json.dumps(notebook), encoding="utf-8")


def _call(args: dict) -> ToolCall:
    return ToolCall(id="smoke-1", name="NotebookEdit", arguments=args)


# ─── Replace / modify ─────────────────────────────────────────────────────────


async def test_replace_existing_code_cell(tmp_path: Path) -> None:
    """Replace mode mutates the target cell source on disk."""
    nb_path = tmp_path / "test.ipynb"
    _build_notebook(nb_path)
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "replace",
                "cell_index": 1,
                "cell_type": "code",
                "source": "x = 42\nprint(x)\n",
            }
        )
    )
    assert not result.is_error, result.content

    data = json.loads(nb_path.read_text())
    assert len(data["cells"]) == 2, "cell count should be unchanged after replace"
    assert data["cells"][1]["source"] == "x = 42\nprint(x)\n"
    assert data["cells"][1]["cell_type"] == "code"


async def test_replace_changes_cell_type(tmp_path: Path) -> None:
    """Replace can switch a code cell to markdown."""
    nb_path = tmp_path / "test.ipynb"
    _build_notebook(nb_path)
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "replace",
                "cell_index": 1,
                "cell_type": "markdown",
                "source": "## Changed to markdown",
            }
        )
    )
    assert not result.is_error, result.content

    data = json.loads(nb_path.read_text())
    assert data["cells"][1]["cell_type"] == "markdown"
    assert "execution_count" not in data["cells"][1], (
        "markdown cells must not have execution_count"
    )


async def test_replace_assigns_fresh_cell_id(tmp_path: Path) -> None:
    """Replaced cell gets a new nbformat-4.5-compliant id (8 hex chars)."""
    nb_path = tmp_path / "test.ipynb"
    _build_notebook(nb_path)
    original_id = json.loads(nb_path.read_text())["cells"][1]["id"]

    tool = NotebookEditTool()
    await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "replace",
                "cell_index": 1,
                "cell_type": "code",
                "source": "pass",
            }
        )
    )

    data = json.loads(nb_path.read_text())
    new_id = data["cells"][1].get("id", "")
    assert len(new_id) == 8, f"cell id should be 8 chars, got {new_id!r}"
    assert new_id != original_id, "replaced cell should have a fresh id"


# ─── Insert ───────────────────────────────────────────────────────────────────


async def test_insert_at_beginning(tmp_path: Path) -> None:
    """Insert at index 0 prepends a new cell."""
    nb_path = tmp_path / "test.ipynb"
    _build_notebook(nb_path)
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "insert",
                "cell_index": 0,
                "cell_type": "code",
                "source": "# prepended",
            }
        )
    )
    assert not result.is_error, result.content

    data = json.loads(nb_path.read_text())
    assert len(data["cells"]) == 3
    assert data["cells"][0]["source"] == "# prepended"
    assert data["cells"][0]["cell_type"] == "code"
    assert "id" in data["cells"][0], "inserted cell must have an id"


async def test_insert_in_middle(tmp_path: Path) -> None:
    """Insert at index 1 places the new cell between existing ones."""
    nb_path = tmp_path / "test.ipynb"
    _build_notebook(nb_path)
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "insert",
                "cell_index": 1,
                "cell_type": "markdown",
                "source": "## inserted middle",
            }
        )
    )
    assert not result.is_error, result.content

    data = json.loads(nb_path.read_text())
    assert len(data["cells"]) == 3
    assert data["cells"][1]["source"] == "## inserted middle"
    # Original markdown still at index 0; original code now at index 2
    assert "Title" in data["cells"][0]["source"][0]
    assert data["cells"][2]["cell_type"] == "code"


async def test_insert_appends_past_end(tmp_path: Path) -> None:
    """Insert at index == len(cells) appends without error."""
    nb_path = tmp_path / "test.ipynb"
    _build_notebook(nb_path)
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "insert",
                "cell_index": 2,  # == len(cells)
                "cell_type": "raw",
                "source": "appended raw cell",
            }
        )
    )
    assert not result.is_error, result.content

    data = json.loads(nb_path.read_text())
    assert len(data["cells"]) == 3
    assert data["cells"][2]["cell_type"] == "raw"
    assert data["cells"][2]["source"] == "appended raw cell"


# ─── Delete ───────────────────────────────────────────────────────────────────


async def test_delete_first_cell(tmp_path: Path) -> None:
    """Delete at index 0 removes the markdown cell."""
    nb_path = tmp_path / "test.ipynb"
    _build_notebook(nb_path)
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "delete",
                "cell_index": 0,
            }
        )
    )
    assert not result.is_error, result.content

    data = json.loads(nb_path.read_text())
    assert len(data["cells"]) == 1
    assert data["cells"][0]["cell_type"] == "code"


async def test_delete_last_cell(tmp_path: Path) -> None:
    """Delete at index 1 (the last cell) leaves only one cell."""
    nb_path = tmp_path / "test.ipynb"
    _build_notebook(nb_path)
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "delete",
                "cell_index": 1,
            }
        )
    )
    assert not result.is_error, result.content

    data = json.loads(nb_path.read_text())
    assert len(data["cells"]) == 1
    assert data["cells"][0]["cell_type"] == "markdown"


async def test_delete_result_message_mentions_cell_count(tmp_path: Path) -> None:
    """ToolResult content reports the new cell count after deletion."""
    nb_path = tmp_path / "test.ipynb"
    _build_notebook(nb_path)
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "delete",
                "cell_index": 0,
            }
        )
    )
    assert not result.is_error, result.content
    assert "1" in result.content, (
        f"result content should mention the new count (1): {result.content!r}"
    )


# ─── Error paths ──────────────────────────────────────────────────────────────


async def test_missing_file_returns_error(tmp_path: Path) -> None:
    """Referencing a non-existent .ipynb path gives is_error=True."""
    tool = NotebookEditTool()
    result = await tool.execute(
        _call(
            {
                "path": str(tmp_path / "nonexistent.ipynb"),
                "mode": "replace",
                "cell_index": 0,
                "cell_type": "code",
                "source": "x = 1",
            }
        )
    )
    assert result.is_error
    assert "not found" in result.content.lower() or "error" in result.content.lower()


async def test_corrupt_notebook_returns_error(tmp_path: Path) -> None:
    """A file with invalid JSON gives is_error=True without raising."""
    nb_path = tmp_path / "corrupt.ipynb"
    nb_path.write_text("not valid json {{{", encoding="utf-8")
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "replace",
                "cell_index": 0,
                "cell_type": "code",
                "source": "x = 1",
            }
        )
    )
    assert result.is_error
    assert "json" in result.content.lower() or "error" in result.content.lower()


async def test_invalid_mode_returns_error(tmp_path: Path) -> None:
    """An unrecognised mode string gives is_error=True."""
    nb_path = tmp_path / "test.ipynb"
    _build_notebook(nb_path)
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "overwrite",  # not a valid mode
                "cell_index": 0,
                "cell_type": "code",
                "source": "x = 1",
            }
        )
    )
    assert result.is_error


async def test_out_of_range_index_returns_error(tmp_path: Path) -> None:
    """A cell_index past the end of the cells list gives is_error=True for replace."""
    nb_path = tmp_path / "test.ipynb"
    _build_notebook(nb_path)
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "replace",
                "cell_index": 99,  # far out of bounds
                "cell_type": "code",
                "source": "x = 1",
            }
        )
    )
    assert result.is_error
    assert "bounds" in result.content.lower() or "error" in result.content.lower()


async def test_missing_cell_type_on_insert_returns_error(tmp_path: Path) -> None:
    """insert without cell_type gives is_error=True."""
    nb_path = tmp_path / "test.ipynb"
    _build_notebook(nb_path)
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "insert",
                "cell_index": 0,
                # cell_type omitted intentionally
                "source": "x = 1",
            }
        )
    )
    assert result.is_error


async def test_non_ipynb_extension_returns_error(tmp_path: Path) -> None:
    """A .txt path is rejected even if it exists."""
    txt_path = tmp_path / "notebook.txt"
    txt_path.write_text("{}", encoding="utf-8")
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(txt_path),
                "mode": "replace",
                "cell_index": 0,
                "cell_type": "code",
                "source": "x = 1",
            }
        )
    )
    assert result.is_error
    assert ".ipynb" in result.content or "error" in result.content.lower()


async def test_negative_cell_index_returns_error(tmp_path: Path) -> None:
    """A negative cell_index is rejected for all modes."""
    nb_path = tmp_path / "test.ipynb"
    _build_notebook(nb_path)
    tool = NotebookEditTool()

    result = await tool.execute(
        _call(
            {
                "path": str(nb_path),
                "mode": "delete",
                "cell_index": -1,
            }
        )
    )
    assert result.is_error
