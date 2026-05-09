"""M4.3 + M4.4 — SKILL.md frontmatter `context: fork` + `tools:` allowlist.

Pins the contract added 2026-05-09:

* `context: inline` (default) — body returned to parent + optional
  advisory `[Skill Tools Constraint]` line when `tools:` set.
* `context: fork` — synthesises a `delegate(task=body, ...)` call.
  Frontmatter `agent` / `tools` / `isolation` flow into delegate args.
* `model:` field with `context: inline` raises
  `SkillModelOverrideRequiresForkError`.
* Unknown `context` value raises a ToolResult error.

These tests stub :class:`DelegateTool` so we don't spin up a real
subagent — the focus is the SkillTool dispatch + frontmatter shape.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from opencomputer.agent.memory import MemoryManager, SkillMeta
from opencomputer.tools.skill import (
    SkillModelOverrideRequiresForkError,
    SkillTool,
)
from plugin_sdk.core import ToolCall, ToolResult


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _seed_skill(
    tmp_path: Path, skill_id: str, body: str, frontmatter_yaml: str = ""
) -> Path:
    """Write a SKILL.md to ``tmp_path/<skill_id>/SKILL.md`` and return its path."""
    skill_dir = tmp_path / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = f"---\n{frontmatter_yaml}---\n" if frontmatter_yaml else ""
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(f"{fm}{body}")
    return skill_path


def _make_tool(tmp_path: Path, skill_id: str, body: str, fm: str = "") -> SkillTool:
    """Build a SkillTool whose memory manager points at ``tmp_path``."""
    skill_path = _seed_skill(tmp_path, skill_id, body, fm)

    # Stub MemoryManager so list_skills returns our seeded skill
    class _StubMemory:
        def list_skills(self) -> list[SkillMeta]:
            return [
                SkillMeta(
                    id=skill_id,
                    name=skill_id,
                    description="",
                    version="1",
                    path=skill_path,
                )
            ]

    return SkillTool(memory_manager=_StubMemory())


# ─── inline mode (default) ───────────────────────────────────────────────


class TestInlineMode:
    def test_no_frontmatter_returns_body(self, tmp_path: Path) -> None:
        tool = _make_tool(tmp_path, "demo", "Step 1.\nStep 2.")
        result = _run(
            tool.execute(ToolCall(id="t1", name="Skill", arguments={"name": "demo"}))
        )
        assert not result.is_error
        assert "# Skill: demo" in result.content
        assert "Step 1." in result.content

    def test_inline_with_tools_renders_advisory_directive(
        self, tmp_path: Path
    ) -> None:
        tool = _make_tool(
            tmp_path,
            "lint",
            "Run the linter.",
            "context: inline\ntools:\n  - Read\n  - Bash\n",
        )
        result = _run(
            tool.execute(ToolCall(id="t1", name="Skill", arguments={"name": "lint"}))
        )
        assert not result.is_error
        assert "[Skill Tools Constraint]" in result.content
        assert "Read, Bash" in result.content
        # M4.4 hard-enforce (2026-05-09): inline tools allowlist now
        # enforced at dispatch; directive now says "hard-blocked".
        assert "hard-blocked" in result.content
        assert "Run the linter." in result.content

    def test_inline_without_tools_omits_directive(self, tmp_path: Path) -> None:
        tool = _make_tool(tmp_path, "demo", "Body.", "context: inline\n")
        result = _run(
            tool.execute(ToolCall(id="t1", name="Skill", arguments={"name": "demo"}))
        )
        assert "[Skill Tools Constraint]" not in result.content


# ─── inline + model raises ───────────────────────────────────────────────


class TestInlineWithModelRaises:
    def test_inline_with_model_raises_specific_error(
        self, tmp_path: Path
    ) -> None:
        tool = _make_tool(
            tmp_path,
            "broken",
            "Body.",
            "context: inline\nmodel: gpt-4o\n",
        )
        with pytest.raises(SkillModelOverrideRequiresForkError) as exc_info:
            _run(
                tool.execute(
                    ToolCall(id="t1", name="Skill", arguments={"name": "broken"})
                )
            )
        assert "context: fork" in str(exc_info.value)


# ─── fork mode ───────────────────────────────────────────────────────────


class TestForkMode:
    def test_fork_dispatches_to_delegate(self, tmp_path: Path) -> None:
        tool = _make_tool(
            tmp_path,
            "explore",
            "Find all TODOs in the codebase.",
            "context: fork\n",
        )

        # Stub DelegateTool so we don't actually spawn a subagent.
        recorded: dict[str, Any] = {}

        class _StubDelegate:
            async def execute(self, call: ToolCall) -> ToolResult:
                recorded["args"] = dict(call.arguments)
                recorded["call_id"] = call.id
                return ToolResult(
                    tool_call_id=call.id,
                    content="Found 3 TODOs.",
                    is_error=False,
                )

        with patch(
            "opencomputer.tools.delegate.DelegateTool",
            return_value=_StubDelegate(),
        ):
            result = _run(
                tool.execute(
                    ToolCall(id="t1", name="Skill", arguments={"name": "explore"})
                )
            )

        assert not result.is_error
        assert "# Skill: explore (forked)" in result.content
        assert "Found 3 TODOs." in result.content
        # Verify the synthetic delegate call carried the body as task
        assert "Find all TODOs" in recorded["args"]["task"]

    def test_fork_threads_agent_and_tools_to_delegate(
        self, tmp_path: Path
    ) -> None:
        tool = _make_tool(
            tmp_path,
            "review",
            "Review the diff.",
            "context: fork\nagent: code-reviewer\ntools:\n  - Read\n  - Grep\n",
        )

        recorded: dict[str, Any] = {}

        class _StubDelegate:
            async def execute(self, call: ToolCall) -> ToolResult:
                recorded["args"] = dict(call.arguments)
                return ToolResult(tool_call_id=call.id, content="reviewed")

        with patch(
            "opencomputer.tools.delegate.DelegateTool",
            return_value=_StubDelegate(),
        ):
            _run(
                tool.execute(
                    ToolCall(id="t1", name="Skill", arguments={"name": "review"})
                )
            )

        assert recorded["args"]["agent"] == "code-reviewer"
        assert recorded["args"]["allowed_tools"] == ["Read", "Grep"]

    def test_fork_threads_isolation_to_delegate(self, tmp_path: Path) -> None:
        tool = _make_tool(
            tmp_path,
            "iso-skill",
            "Do work.",
            "context: fork\nisolation: copy\n",
        )

        recorded: dict[str, Any] = {}

        class _StubDelegate:
            async def execute(self, call: ToolCall) -> ToolResult:
                recorded["args"] = dict(call.arguments)
                return ToolResult(tool_call_id=call.id, content="ok")

        with patch(
            "opencomputer.tools.delegate.DelegateTool",
            return_value=_StubDelegate(),
        ):
            _run(
                tool.execute(
                    ToolCall(id="t1", name="Skill", arguments={"name": "iso-skill"})
                )
            )

        assert recorded["args"]["isolation"] == "copy"

    def test_fork_invalid_isolation_returns_error(self, tmp_path: Path) -> None:
        tool = _make_tool(
            tmp_path,
            "bad-iso",
            "Do work.",
            "context: fork\nisolation: kubernetes\n",
        )

        result = _run(
            tool.execute(ToolCall(id="t1", name="Skill", arguments={"name": "bad-iso"}))
        )
        assert result.is_error
        assert "kubernetes" in result.content


# ─── unknown context ─────────────────────────────────────────────────────


class TestUnknownContext:
    def test_unknown_context_returns_error(self, tmp_path: Path) -> None:
        tool = _make_tool(
            tmp_path,
            "weird",
            "Body.",
            "context: zoo\n",
        )
        result = _run(
            tool.execute(ToolCall(id="t1", name="Skill", arguments={"name": "weird"}))
        )
        assert result.is_error
        assert "context='zoo'" in result.content or "context=zoo" in result.content


# ─── error paths from existing surface still work ────────────────────────


class TestExistingErrorPaths:
    def test_missing_name_returns_error(self, tmp_path: Path) -> None:
        tool = _make_tool(tmp_path, "demo", "body")
        result = _run(
            tool.execute(ToolCall(id="t1", name="Skill", arguments={}))
        )
        assert result.is_error
        assert "name is required" in result.content

    def test_unknown_skill_returns_error(self, tmp_path: Path) -> None:
        tool = _make_tool(tmp_path, "demo", "body")
        result = _run(
            tool.execute(
                ToolCall(id="t1", name="Skill", arguments={"name": "ghost"})
            )
        )
        assert result.is_error
        assert "ghost" in result.content
