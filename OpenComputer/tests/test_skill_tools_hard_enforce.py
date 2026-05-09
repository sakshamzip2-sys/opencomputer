"""M4.4 hard enforcement — inline SKILL.md `tools:` allowlist.

Pins the contract added 2026-05-09:

* When an inline skill declares `tools:`, calling SkillTool sets
  the process-wide filter via :mod:`opencomputer.agent.skill_tools_filter`.
* :func:`is_tool_allowed` returns False for tools outside the
  allowlist; the agent loop's `_dispatch_tool_calls` builds a
  blocked entry that produces an `is_error=True` ToolResult.
* The `Skill` tool itself is implicitly allowed even when filter
  is active (so the model can switch skills without self-blocking).
* :func:`clear_active_filter` drops the filter (called by the loop
  on END_TURN).
* The Skill tool result body still surfaces the constraint so the
  model knows what's blocked.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys as _sys
from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.memory import SkillMeta
from opencomputer.agent.skill_tools_filter import (
    ActiveSkillFilter,
    clear_active_filter,
    get_active_filter,
    is_tool_allowed,
    set_active_filter,
)
from plugin_sdk.core import ToolCall

# Load SkillTool via the plugin-loader-style synthetic name pattern.
_SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "opencomputer"
    / "tools"
    / "skill.py"
)
_spec = importlib.util.spec_from_file_location(
    "_test_skill_tool_module", _SKILL_PATH
)
skill_module = importlib.util.module_from_spec(_spec)
_sys.modules["_test_skill_tool_module"] = skill_module
_spec.loader.exec_module(skill_module)
SkillTool = skill_module.SkillTool


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _reset_filter() -> None:
    """Each test starts with an empty filter slot."""
    clear_active_filter()
    yield
    clear_active_filter()


def _make_tool(tmp_path: Path, body: str, fm: str = "") -> SkillTool:
    """Build a SkillTool whose memory manager points at a synthetic skill."""
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm_block = f"---\n{fm}---\n" if fm else ""
    (skill_dir / "SKILL.md").write_text(f"{fm_block}{body}")

    class _StubMemory:
        def list_skills(self) -> list[SkillMeta]:
            return [
                SkillMeta(
                    id="demo",
                    name="demo",
                    description="",
                    version="1",
                    path=skill_dir / "SKILL.md",
                )
            ]

    return SkillTool(memory_manager=_StubMemory())


# ─── ActiveSkillFilter slot contract ─────────────────────────────────────


class TestFilterSlot:
    def test_no_filter_default(self) -> None:
        assert get_active_filter() is None

    def test_set_and_get(self) -> None:
        set_active_filter("demo", ["Read", "Grep"])
        flt = get_active_filter()
        assert isinstance(flt, ActiveSkillFilter)
        assert flt.skill_name == "demo"
        assert flt.allowed_tools == frozenset({"Read", "Grep"})

    def test_set_replaces(self) -> None:
        set_active_filter("a", ["X"])
        set_active_filter("b", ["Y"])
        assert get_active_filter().skill_name == "b"

    def test_clear_returns_prior(self) -> None:
        set_active_filter("demo", ["Read"])
        prior = clear_active_filter()
        assert prior is not None
        assert prior.skill_name == "demo"
        assert get_active_filter() is None


# ─── is_tool_allowed ─────────────────────────────────────────────────────


class TestIsToolAllowed:
    def test_no_filter_allows_everything(self) -> None:
        allowed, reason = is_tool_allowed("Bash")
        assert allowed is True
        assert reason is None

    def test_in_allowlist_allowed(self) -> None:
        set_active_filter("demo", ["Read", "Grep"])
        allowed, reason = is_tool_allowed("Read")
        assert allowed is True
        assert reason is None

    def test_outside_allowlist_blocked(self) -> None:
        set_active_filter("demo", ["Read"])
        allowed, reason = is_tool_allowed("Bash")
        assert allowed is False
        assert reason is not None
        assert "demo" in reason
        assert "Bash" in reason

    def test_empty_allowlist_blocks_all(self) -> None:
        set_active_filter("locked", [])
        allowed, reason = is_tool_allowed("Read")
        assert allowed is False
        assert "(none)" in reason


# ─── SkillTool sets the filter on inline + tools ─────────────────────────


class TestSkillToolSetsFilter:
    def test_inline_with_tools_activates_filter(self, tmp_path: Path) -> None:
        tool = _make_tool(
            tmp_path,
            "Run lint then format.",
            "context: inline\ntools:\n  - Read\n  - Bash\n",
        )
        result = _run(
            tool.execute(ToolCall(id="t", name="Skill", arguments={"name": "demo"}))
        )
        assert not result.is_error
        flt = get_active_filter()
        assert flt is not None
        assert flt.skill_name == "demo"
        assert flt.allowed_tools == frozenset({"Read", "Bash"})

    def test_inline_without_tools_no_filter(self, tmp_path: Path) -> None:
        tool = _make_tool(tmp_path, "Body.", "context: inline\n")
        _run(
            tool.execute(ToolCall(id="t", name="Skill", arguments={"name": "demo"}))
        )
        assert get_active_filter() is None

    def test_inline_with_empty_tools_no_filter(self, tmp_path: Path) -> None:
        # tools: [] → no filter activated (empty list isn't truthy)
        tool = _make_tool(tmp_path, "Body.", "context: inline\ntools: []\n")
        _run(
            tool.execute(ToolCall(id="t", name="Skill", arguments={"name": "demo"}))
        )
        assert get_active_filter() is None

    def test_body_includes_hard_enforce_directive(self, tmp_path: Path) -> None:
        tool = _make_tool(
            tmp_path,
            "Body.",
            "context: inline\ntools:\n  - Read\n",
        )
        result = _run(
            tool.execute(ToolCall(id="t", name="Skill", arguments={"name": "demo"}))
        )
        # The body's directive now says "hard-blocked" (not "advisory")
        assert "hard-blocked" in result.content
        assert "Read" in result.content


# ─── End-to-end: filter blocks dispatch in agent loop ────────────────────
# (Driven via skill_tools_filter directly since spinning up an AgentLoop
# in unit tests is heavy; the loop integration is covered by the
# is_tool_allowed contract + the loop's _dispatch_tool_calls reading it.)


class TestDispatchIntegration:
    def test_filter_persists_until_cleared(self) -> None:
        set_active_filter("demo", ["Read"])
        # Stays active across multiple is_tool_allowed checks
        for _ in range(5):
            allowed, _ = is_tool_allowed("Bash")
            assert allowed is False
        # Until cleared
        clear_active_filter()
        allowed, _ = is_tool_allowed("Bash")
        assert allowed is True

    def test_skill_tool_name_implicitly_allowed(self) -> None:
        # The agent loop's dispatch path explicitly skips the filter
        # for the "Skill" tool name so the model can swap skills.
        # is_tool_allowed itself doesn't carve out Skill — that's
        # a loop-side decision. Verify the slot says block, but the
        # loop-side carve-out can override.
        set_active_filter("demo", ["Read"])
        allowed, _ = is_tool_allowed("Skill")
        # is_tool_allowed reports the raw answer (Skill not in allowlist)
        assert allowed is False
        # The loop's dispatch carve-out (which we test via inspecting
        # the source) must skip the check for c.name == "Skill".
        from pathlib import Path

        loop_src = Path("opencomputer/agent/loop.py").read_text()
        assert 'if c.name == "Skill":' in loop_src
