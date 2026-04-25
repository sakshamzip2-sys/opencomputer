"""Tests for use_cases.personal_knowledge_management.

Covers:
- index_recent_notes filters results to .md / .txt / .org extensions only
- index_recent_notes returns expected shape
- search_notes delegates to SearchFilesTool correctly
- search_notes returns list of dicts
- extract_action_items finds unchecked checkboxes (- [ ] and * [ ])
- extract_action_items finds inline TODOs
- extract_action_items handles empty / error content
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from extensions.oi_capability.use_cases.personal_knowledge_management import (
    extract_action_items,
    index_recent_notes,
    search_notes,
)

from plugin_sdk.core import ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wrapper():
    w = MagicMock()
    w.call = AsyncMock(return_value={})
    return w


def _tool_result(content="", *, is_error=False):
    return ToolResult(tool_call_id="t", content=content, is_error=is_error)


# ---------------------------------------------------------------------------
# index_recent_notes
# ---------------------------------------------------------------------------

class TestIndexRecentNotes:
    async def test_filters_to_note_extensions(self):
        """Only .md/.txt/.org files should be included in 'notes'."""
        raw_output = (
            "/home/user/notes/draft.md\n"
            "/home/user/code/main.py\n"
            "/home/user/notes/todo.txt\n"
            "/home/user/notes/journal.org\n"
            "/home/user/Downloads/image.png\n"
            "/tmp/cache.json\n"
        )

        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ListRecentFilesTool.execute",
            new=AsyncMock(return_value=_tool_result(raw_output)),
        ):
            result = await index_recent_notes(_make_wrapper())

        note_paths = result["notes"]
        assert all(
            fp.endswith(".md") or fp.endswith(".txt") or fp.endswith(".org")
            for fp in note_paths
        )
        assert not any(fp.endswith(".py") for fp in note_paths)
        assert not any(fp.endswith(".png") for fp in note_paths)

    async def test_returns_expected_shape(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ListRecentFilesTool.execute",
            new=AsyncMock(return_value=_tool_result("/home/user/notes/a.md\n")),
        ):
            result = await index_recent_notes(_make_wrapper())

        assert "notes" in result
        assert "count" in result
        assert "extensions_found" in result
        assert isinstance(result["notes"], list)
        assert isinstance(result["count"], int)

    async def test_count_matches_notes_length(self):
        raw = "/a.md\n/b.txt\n/c.org\n/d.py\n"
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ListRecentFilesTool.execute",
            new=AsyncMock(return_value=_tool_result(raw)),
        ):
            result = await index_recent_notes(_make_wrapper())

        assert result["count"] == len(result["notes"])

    async def test_returns_empty_on_error(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ListRecentFilesTool.execute",
            new=AsyncMock(return_value=_tool_result("err", is_error=True)),
        ):
            result = await index_recent_notes(_make_wrapper())

        assert result["notes"] == []
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# search_notes
# ---------------------------------------------------------------------------

class TestSearchNotes:
    async def test_delegates_to_search_files(self):
        call_args = {}

        async def _exec(self_tool, call):
            call_args.update(call.arguments)
            return _tool_result("['note.md']")

        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.SearchFilesTool.execute",
            new=_exec,
        ):
            result = await search_notes(_make_wrapper(), "project plan")

        assert call_args.get("query") == "project plan"
        assert isinstance(result, list)

    async def test_returns_list_of_dicts(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.SearchFilesTool.execute",
            new=AsyncMock(return_value=_tool_result("['note.md', 'meeting.org']")),
        ):
            result = await search_notes(_make_wrapper(), "query")

        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, dict)
            assert "path" in item

    async def test_returns_empty_on_error(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.SearchFilesTool.execute",
            new=AsyncMock(return_value=_tool_result("err", is_error=True)),
        ):
            result = await search_notes(_make_wrapper(), "query")

        assert result == []


# ---------------------------------------------------------------------------
# extract_action_items
# ---------------------------------------------------------------------------

CHECKBOX_CONTENT = """\
# Project Notes

- [ ] Write unit tests
- [x] Set up CI
* [ ] Review PR #42
- [ ] Update documentation

Some prose here.
TODO: Fix the login bug
TODO: Deploy to staging

* [x] Already done item (should be ignored)
"""

JUST_TODOS = """\
TODO: Add error handling
TODO: refactor the DB module
Regular line without action items.
"""

EMPTY_CONTENT = ""


@pytest.mark.parametrize(
    "content, expected_items",
    [
        (
            CHECKBOX_CONTENT,
            ["Write unit tests", "Review PR #42", "Update documentation"],
        ),
        (
            JUST_TODOS,
            ["Add error handling", "refactor the DB module"],
        ),
        (
            EMPTY_CONTENT,
            [],
        ),
    ],
    ids=["checkboxes_and_todos", "todos_only", "empty"],
)
class TestExtractActionItems:
    async def test_extracts_expected_items(self, content, expected_items):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ReadFileRegionTool.execute",
            new=AsyncMock(return_value=_tool_result(content)),
        ):
            result = await extract_action_items(_make_wrapper(), "/notes/file.md")

        for expected in expected_items:
            assert any(expected in item for item in result), (
                f"Expected '{expected}' in {result}"
            )

    async def test_returns_list(self, content, expected_items):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ReadFileRegionTool.execute",
            new=AsyncMock(return_value=_tool_result(content)),
        ):
            result = await extract_action_items(_make_wrapper(), "/notes/file.md")

        assert isinstance(result, list)
