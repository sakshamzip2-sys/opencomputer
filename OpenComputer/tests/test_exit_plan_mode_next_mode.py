"""M5.4 — ExitPlanMode `next_mode` suggestion + proposal slot.

Pins the contract added 2026-05-09:

* ``next_mode`` accepts only the 4 canonical values (auto, acceptEdits,
  manual, keep). Anything else returns a tool error.
* When set, the tool stores the proposal in a process-wide slot
  readable via :func:`get_last_proposal` and consumable via
  :func:`pop_last_proposal`.
* The tool result body surfaces the suggested mode in a ``**Suggested
  next_mode:**`` line so the human reader sees it.
* Without ``next_mode``, the proposal slot is unchanged (the tool only
  stores when explicitly suggested).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys as _sys
from pathlib import Path

import pytest

from plugin_sdk.core import ToolCall

# Load exit_plan_mode under a synthetic module name so we don't pollute
# sys.modules with a top-level `tools` namespace — which would collide
# with the real `opencomputer.tools` and break sibling plugin tests
# (CLAUDE.md gotcha #1: plugin module-cache collisions).
_EXIT_PLAN_PATH = (
    Path(__file__).resolve().parents[1]
    / "extensions"
    / "coding-harness"
    / "tools"
    / "exit_plan_mode.py"
)
_spec = importlib.util.spec_from_file_location(
    "_test_exit_plan_mode", _EXIT_PLAN_PATH
)
exit_plan_mode_module = importlib.util.module_from_spec(_spec)
# dataclasses.dataclass walks sys.modules looking up the module by
# name; register the synthetic module under that name so frozen
# dataclass instantiation inside the loaded module doesn't crash.
_sys.modules["_test_exit_plan_mode"] = exit_plan_mode_module
_spec.loader.exec_module(exit_plan_mode_module)
ExitPlanModeTool = exit_plan_mode_module.ExitPlanModeTool
PROPOSED_EXIT_MODES = exit_plan_mode_module.PROPOSED_EXIT_MODES
get_last_proposal = exit_plan_mode_module.get_last_proposal
pop_last_proposal = exit_plan_mode_module.pop_last_proposal


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _reset_proposal_slot() -> None:
    """Each test starts with a clean proposal slot."""
    pop_last_proposal()
    yield
    pop_last_proposal()


# ─── PROPOSED_EXIT_MODES constant ─────────────────────────────────────────


class TestConstants:
    def test_proposed_exit_modes_pins_canonical_set(self) -> None:
        # Pin so adding a new mode stays intentional
        expected = ("auto", "acceptEdits", "manual", "keep")
        assert expected == PROPOSED_EXIT_MODES


# ─── tool execute happy path ──────────────────────────────────────────────


class TestExecuteHappyPath:
    def test_no_next_mode_returns_plain_wrapper(self) -> None:
        tool = ExitPlanModeTool()
        result = _run(
            tool.execute(
                ToolCall(id="t1", name="ExitPlanMode", arguments={"plan": "Do X."})
            )
        )
        assert not result.is_error
        assert "Plan ready for review" in result.content
        assert "Do X." in result.content
        assert "Suggested next_mode" not in result.content
        # No proposal recorded
        assert get_last_proposal() is None

    @pytest.mark.parametrize(
        "mode", ["auto", "acceptEdits", "manual", "keep"]
    )
    def test_next_mode_records_proposal_and_renders_line(
        self, mode: str
    ) -> None:
        tool = ExitPlanModeTool()
        result = _run(
            tool.execute(
                ToolCall(
                    id="t1",
                    name="ExitPlanMode",
                    arguments={"plan": "Refactor auth.", "next_mode": mode},
                )
            )
        )
        assert not result.is_error
        assert "Plan ready for review" in result.content
        assert "Refactor auth." in result.content
        assert f"**Suggested next_mode:** `{mode}`" in result.content
        # Proposal recorded in slot
        proposal = get_last_proposal()
        assert proposal is not None
        assert proposal.plan == "Refactor auth."
        assert proposal.next_mode == mode

    def test_pop_last_proposal_clears_slot(self) -> None:
        tool = ExitPlanModeTool()
        _run(
            tool.execute(
                ToolCall(
                    id="t1",
                    name="ExitPlanMode",
                    arguments={"plan": "x", "next_mode": "auto"},
                )
            )
        )
        first = pop_last_proposal()
        assert first is not None
        assert first.next_mode == "auto"
        # Second pop returns None (slot cleared)
        assert pop_last_proposal() is None


# ─── invalid input ───────────────────────────────────────────────────────


class TestInvalidInput:
    def test_empty_plan_returns_error(self) -> None:
        tool = ExitPlanModeTool()
        result = _run(
            tool.execute(
                ToolCall(id="t1", name="ExitPlanMode", arguments={"plan": ""})
            )
        )
        assert result.is_error
        assert "non-empty string" in result.content

    def test_unknown_next_mode_returns_error(self) -> None:
        tool = ExitPlanModeTool()
        result = _run(
            tool.execute(
                ToolCall(
                    id="t1",
                    name="ExitPlanMode",
                    arguments={"plan": "x", "next_mode": "yolo"},
                )
            )
        )
        assert result.is_error
        assert "next_mode must be one of" in result.content
        assert "yolo" in result.content
        # No proposal recorded on error
        assert get_last_proposal() is None

    def test_non_string_next_mode_returns_error(self) -> None:
        tool = ExitPlanModeTool()
        result = _run(
            tool.execute(
                ToolCall(
                    id="t1",
                    name="ExitPlanMode",
                    arguments={"plan": "x", "next_mode": 42},  # type: ignore[dict-item]
                )
            )
        )
        assert result.is_error
        assert "next_mode must be one of" in result.content


# ─── schema includes next_mode enum ──────────────────────────────────────


class TestSchema:
    def test_schema_exposes_next_mode_enum(self) -> None:
        tool = ExitPlanModeTool()
        params = tool.schema.parameters
        assert "next_mode" in params["properties"]
        nm = params["properties"]["next_mode"]
        assert set(nm["enum"]) == set(PROPOSED_EXIT_MODES)
        # plan stays required, next_mode does not
        assert "next_mode" not in params["required"]
        assert "plan" in params["required"]


# ─── M5.4 follow-up: AgentLoop._maybe_apply_exit_plan_proposal ───────────


class TestLoopApplyExitPlanProposal:
    """Pin AgentLoop._maybe_apply_exit_plan_proposal — the runtime
    mutation surface that consumes the proposal slot after an
    ExitPlanMode tool call lands.

    These tests build a minimal AgentLoop stand-in (just the bits the
    helper touches) so we don't have to spin up the whole loop.
    """

    def _make_stub(self):
        """Return an object with `_runtime` we can mutate via the helper."""
        from dataclasses import replace as _replace

        from plugin_sdk.runtime_context import (
            DEFAULT_RUNTIME_CONTEXT,
            RuntimeContext,
        )

        class _Stub:
            _runtime = _replace(DEFAULT_RUNTIME_CONTEXT, plan_mode=True)

        from opencomputer.agent.loop import AgentLoop

        # Bind the helper to a stub instance — Python's bound-method
        # protocol works on duck-typed objects as long as the
        # function's signature only touches `self._runtime`.
        stub = _Stub()
        stub._maybe_apply_exit_plan_proposal = (
            AgentLoop._maybe_apply_exit_plan_proposal.__get__(stub, _Stub)
        )
        return stub, RuntimeContext

    def test_no_proposal_is_noop(self) -> None:
        stub, _ = self._make_stub()
        before = stub._runtime
        stub._maybe_apply_exit_plan_proposal()
        assert stub._runtime is before

    def test_proposal_keep_does_not_mutate_runtime(self) -> None:
        stub, _ = self._make_stub()
        tool = ExitPlanModeTool()
        _run(
            tool.execute(
                ToolCall(
                    id="t",
                    name="ExitPlanMode",
                    arguments={"plan": "x", "next_mode": "keep"},
                )
            )
        )
        before = stub._runtime
        stub._maybe_apply_exit_plan_proposal()
        assert stub._runtime is before

    def test_proposal_auto_mutates_runtime(self) -> None:
        stub, _ = self._make_stub()
        tool = ExitPlanModeTool()
        _run(
            tool.execute(
                ToolCall(
                    id="t",
                    name="ExitPlanMode",
                    arguments={"plan": "x", "next_mode": "auto"},
                )
            )
        )
        stub._maybe_apply_exit_plan_proposal()
        assert stub._runtime.plan_mode is False
        assert stub._runtime.permission_mode == "auto"

    def test_proposal_accept_edits_mutates_runtime(self) -> None:
        stub, _ = self._make_stub()
        tool = ExitPlanModeTool()
        _run(
            tool.execute(
                ToolCall(
                    id="t",
                    name="ExitPlanMode",
                    arguments={"plan": "x", "next_mode": "acceptEdits"},
                )
            )
        )
        stub._maybe_apply_exit_plan_proposal()
        assert stub._runtime.permission_mode == "acceptEdits"

    def test_proposal_consumed_only_once(self) -> None:
        stub, _ = self._make_stub()
        tool = ExitPlanModeTool()
        _run(
            tool.execute(
                ToolCall(
                    id="t",
                    name="ExitPlanMode",
                    arguments={"plan": "x", "next_mode": "auto"},
                )
            )
        )
        stub._maybe_apply_exit_plan_proposal()
        before = stub._runtime
        stub._maybe_apply_exit_plan_proposal()
        assert stub._runtime is before
