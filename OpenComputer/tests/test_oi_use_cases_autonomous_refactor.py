"""Tests for use_cases.autonomous_refactor.

Covers:
- plan_refactor returns expected shape with candidates + estimated_changes
- plan_refactor returns empty candidates on wrapper error
- execute_refactor_dry_run NEVER calls EditFileTool.execute (verified via Mock spec)
- execute_refactor_dry_run reads each candidate
- execute_refactor without confirm=True raises ValueError
- execute_refactor with confirm=True calls EditFileTool for each candidate
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from extensions.oi_capability.use_cases.autonomous_refactor import (
    execute_refactor,
    execute_refactor_dry_run,
    plan_refactor,
)

from plugin_sdk.core import ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wrapper(result=None, raises=None):
    wrapper = MagicMock()
    if raises is not None:
        wrapper.call = AsyncMock(side_effect=raises)
    else:
        wrapper.call = AsyncMock(return_value=result if result is not None else {})
    return wrapper


def _tool_result(content="", *, is_error=False):
    return ToolResult(tool_call_id="t", content=content, is_error=is_error)


# ---------------------------------------------------------------------------
# plan_refactor
# ---------------------------------------------------------------------------

class TestPlanRefactor:
    async def test_returns_expected_shape(self):
        """plan_refactor must return dict with 'candidates' and 'estimated_changes'."""
        wrapper = _make_wrapper(result=["file1.py", "file2.py"])

        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.SearchFilesTool.execute",
            new=AsyncMock(return_value=_tool_result("['file1.py', 'file2.py']")),
        ):
            result = await plan_refactor(wrapper, "/src", "rename MyClass to NewClass")

        assert "candidates" in result
        assert "estimated_changes" in result
        assert isinstance(result["candidates"], list)
        assert isinstance(result["estimated_changes"], int)

    async def test_estimated_changes_matches_candidates_count(self):
        candidates = ["a.py", "b.py", "c.py"]
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.SearchFilesTool.execute",
            new=AsyncMock(return_value=_tool_result(str(candidates))),
        ):
            result = await plan_refactor(_make_wrapper(), "/src", "query")

        assert result["estimated_changes"] == len(result["candidates"])

    async def test_returns_empty_on_wrapper_error(self):
        wrapper = _make_wrapper(raises=RuntimeError("subprocess died"))
        result = await plan_refactor(wrapper, "/src", "anything")
        assert result["candidates"] == []
        assert result["estimated_changes"] == 0

    async def test_returns_empty_on_tool_error_result(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.SearchFilesTool.execute",
            new=AsyncMock(return_value=_tool_result("error msg", is_error=True)),
        ):
            result = await plan_refactor(_make_wrapper(), "/src", "query")
        assert result["candidates"] == []


# ---------------------------------------------------------------------------
# execute_refactor_dry_run
# ---------------------------------------------------------------------------

class TestExecuteRefactorDryRun:
    async def test_never_calls_edit_file(self):
        """Dry-run MUST NOT call EditFileTool.execute — verified via patch."""
        plan = {"candidates": ["/src/a.py", "/src/b.py"]}

        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ReadFileRegionTool.execute",
            new=AsyncMock(return_value=_tool_result("content here")),
        ) as read_mock:
            with patch(
                "extensions.oi_capability.tools.tier_4_system_control.EditFileTool.execute",
            ) as edit_mock:
                result = await execute_refactor_dry_run(_make_wrapper(), plan)

        edit_mock.assert_not_called()
        assert "would_change" in result
        assert "preview" in result

    async def test_reads_each_candidate(self):
        plan = {"candidates": ["/src/a.py", "/src/b.py"]}
        read_calls: list[str] = []

        async def _read(self_tool, call):
            read_calls.append(call.arguments["path"])
            return _tool_result("some content")

        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ReadFileRegionTool.execute",
            new=_read,
        ):
            result = await execute_refactor_dry_run(_make_wrapper(), plan)

        assert "/src/a.py" in read_calls
        assert "/src/b.py" in read_calls
        assert len(result["would_change"]) == 2

    async def test_returns_correct_shape(self):
        plan = {"candidates": ["/src/x.py"]}

        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ReadFileRegionTool.execute",
            new=AsyncMock(return_value=_tool_result("class Foo: pass")),
        ):
            result = await execute_refactor_dry_run(_make_wrapper(), plan)

        assert isinstance(result["would_change"], list)
        assert isinstance(result["preview"], dict)

    async def test_empty_plan_returns_empty(self):
        plan = {"candidates": []}
        result = await execute_refactor_dry_run(_make_wrapper(), plan)
        assert result["would_change"] == []
        assert result["preview"] == {}


# ---------------------------------------------------------------------------
# execute_refactor
# ---------------------------------------------------------------------------

class TestExecuteRefactor:
    async def test_raises_without_confirm(self):
        plan = {"candidates": ["/src/a.py"]}
        with pytest.raises(ValueError, match="confirm=True"):
            await execute_refactor(_make_wrapper(), plan)

    async def test_raises_with_confirm_false(self):
        plan = {"candidates": ["/src/a.py"]}
        with pytest.raises(ValueError, match="confirm=True"):
            await execute_refactor(_make_wrapper(), plan, confirm=False)

    async def test_calls_edit_file_for_each_candidate_with_confirm(self):
        plan = {"candidates": ["/src/a.py", "/src/b.py"]}
        edit_calls: list[str] = []

        async def _edit(self_tool, call):
            edit_calls.append(call.arguments["path"])
            return _tool_result("ok")

        with patch(
            "extensions.oi_capability.tools.tier_4_system_control.EditFileTool.execute",
            new=_edit,
        ):
            result = await execute_refactor(_make_wrapper(), plan, confirm=True)

        assert "/src/a.py" in edit_calls
        assert "/src/b.py" in edit_calls
        assert len(result["changed"]) == 2
        assert result["errors"] == []

    async def test_returns_correct_shape(self):
        plan = {"candidates": []}
        result = await execute_refactor(_make_wrapper(), plan, confirm=True)
        assert "changed" in result
        assert "errors" in result
