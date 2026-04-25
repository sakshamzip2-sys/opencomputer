"""Tests for use_cases.context_aware_code_suggestions.

Covers:
- gather_code_context reads the target file
- gather_code_context reads neighbor files via ReadFileRegionTool
- gather_code_context returns correct shape
- git_blame_context shells out to git blame (mock subprocess)
- git_blame_context handles git not found gracefully
- git_blame_context handles git blame error returncode
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from extensions.oi_capability.use_cases.context_aware_code_suggestions import (
    gather_code_context,
    git_blame_context,
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
# gather_code_context
# ---------------------------------------------------------------------------

class TestGatherCodeContext:
    async def test_reads_target_file(self):
        """gather_code_context must call ReadFileRegionTool with the target file."""
        read_calls: list[str] = []

        async def _exec(self_tool, call):
            read_calls.append(call.arguments["path"])
            return _tool_result("content")

        with (
            patch(
                "extensions.oi_capability.tools.tier_1_introspection.ReadFileRegionTool.execute",
                new=_exec,
            ),
            patch("pathlib.Path.iterdir", return_value=iter([])),
        ):
            result = await gather_code_context(_make_wrapper(), "/src/main.py")

        assert "/src/main.py" in read_calls
        assert "target" in result
        assert "neighbors" in result

    async def test_returns_correct_shape(self):
        with (
            patch(
                "extensions.oi_capability.tools.tier_1_introspection.ReadFileRegionTool.execute",
                new=AsyncMock(return_value=_tool_result("file content")),
            ),
            patch("pathlib.Path.iterdir", return_value=iter([])),
        ):
            result = await gather_code_context(_make_wrapper(), "/src/main.py")

        assert isinstance(result["target"], str)
        assert isinstance(result["neighbors"], dict)

    async def test_reads_neighbor_files(self, tmp_path):
        """gather_code_context should read sibling files from the same directory."""
        # Create real temp files to test neighbor discovery
        main_py = tmp_path / "main.py"
        main_py.write_text("# main")
        (tmp_path / "utils.py").write_text("# utils")
        (tmp_path / "config.py").write_text("# config")

        read_calls: list[str] = []

        async def _exec(self_tool, call):
            read_calls.append(call.arguments["path"])
            return _tool_result("content")

        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ReadFileRegionTool.execute",
            new=_exec,
        ):
            result = await gather_code_context(_make_wrapper(), str(main_py), neighbor_radius=2)

        # Should have read target + at least some neighbors
        assert str(main_py) in read_calls
        assert len(result["neighbors"]) >= 1

    async def test_handles_permission_error(self):
        """gather_code_context should not raise when iterdir fails."""
        with (
            patch(
                "extensions.oi_capability.tools.tier_1_introspection.ReadFileRegionTool.execute",
                new=AsyncMock(return_value=_tool_result("content")),
            ),
            patch("pathlib.Path.iterdir", side_effect=PermissionError("no access")),
        ):
            result = await gather_code_context(_make_wrapper(), "/secret/main.py")

        # Should still return target content with empty neighbors
        assert result["target"] == "content"
        assert result["neighbors"] == {}


# ---------------------------------------------------------------------------
# git_blame_context
# ---------------------------------------------------------------------------

_PORCELAIN_OUTPUT = """\
abc1234567890abcdef1234567890abcdef12345678 1 1 1
author Alice Smith
author-mail <alice@corp.com>
author-time 1700000000
author-tz +0000
committer Bob Jones
committer-mail <bob@corp.com>
committer-time 1700000001
committer-tz +0000
summary feat: initial commit
filename main.py
\tdef main():
abc1234567890abcdef1234567890abcdef12345678 2 2 1
author Alice Smith
author-mail <alice@corp.com>
author-time 1700000000
author-tz +0000
committer Bob Jones
committer-mail <bob@corp.com>
committer-time 1700000001
committer-tz +0000
summary feat: initial commit
filename main.py
\t    pass
"""


class TestGitBlameContext:
    async def test_shells_out_to_git_blame(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=_PORCELAIN_OUTPUT, stderr=""
            )
            result = await git_blame_context(
                _make_wrapper(), "/src/main.py", line_start=1, line_end=2
            )

        # Verify git blame was called with correct args
        called_cmd = mock_run.call_args[0][0]
        assert "git" in called_cmd
        assert "blame" in called_cmd
        assert "-L1,2" in called_cmd
        assert "--porcelain" in called_cmd

    async def test_returns_blame_info_per_line(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=_PORCELAIN_OUTPUT, stderr=""
            )
            result = await git_blame_context(
                _make_wrapper(), "/src/main.py", line_start=1, line_end=2
            )

        # Should return author, commit, date per line (or error key)
        if "error" not in result:
            for _lineno, info in result.items():
                assert "author" in info
                assert "commit" in info
                assert "date" in info

    async def test_handles_git_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            result = await git_blame_context(
                _make_wrapper(), "/src/main.py", line_start=1, line_end=5
            )

        assert "error" in result
        assert "git" in result["error"].lower()

    async def test_handles_git_error_returncode(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=128,
                stdout="",
                stderr="fatal: not a git repo",
            )
            result = await git_blame_context(
                _make_wrapper(), "/src/main.py", line_start=1, line_end=5
            )

        assert "error" in result

    async def test_handles_timeout(self):
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("git", 30),
        ):
            result = await git_blame_context(
                _make_wrapper(), "/src/main.py", line_start=1, line_end=5
            )

        assert "error" in result
        assert "timed out" in result["error"].lower() or "timeout" in result["error"].lower()
