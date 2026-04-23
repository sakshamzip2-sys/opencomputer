"""Phase 12b.4 (Task D5) — ExitPlanMode tool.

Claude Code parity. The agent calls ``ExitPlanMode(plan="...")`` when it
has finished formulating a plan in plan mode. The tool returns the plan
wrapped in a user-visible "Plan ready for review / Awaiting user approval"
header.

The tool does NOT mutate RuntimeContext (frozen) — it's a signal, not a
state change. The user exits plan mode out-of-band (/exit-plan slash
command in D8 or by re-running without ``--plan``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from plugin_sdk.core import ToolCall
from plugin_sdk.tool_contract import BaseTool, ToolSchema

# ─── Locate the tool module via a unique importlib spec so we don't
#     collide with other plugin `tools.exit_plan_mode` caches. Mirrors
#     the pattern used in opencomputer.plugins.loader.
REPO_ROOT = Path(__file__).resolve().parent.parent
EXIT_PLAN_PATH = (
    REPO_ROOT / "extensions" / "coding-harness" / "tools" / "exit_plan_mode.py"
)


def _load_exit_plan_module():
    spec = importlib.util.spec_from_file_location(
        "_t_phase12b4_exit_plan_mode",
        EXIT_PLAN_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_t_phase12b4_exit_plan_mode"] = module
    spec.loader.exec_module(module)
    return module


# ─── Test 1 — schema ────────────────────────────────────────────────────


def test_schema_name_and_required_plan() -> None:
    """Schema exposes name 'ExitPlanMode' and `plan` is required."""
    mod = _load_exit_plan_module()
    tool = mod.ExitPlanModeTool()
    schema: ToolSchema = tool.schema
    assert isinstance(tool, BaseTool)
    assert schema.name == "ExitPlanMode"
    params = schema.parameters
    assert params["type"] == "object"
    assert "plan" in params["properties"]
    assert params["properties"]["plan"]["type"] == "string"
    assert params["required"] == ["plan"]


# ─── Test 2 — happy path wraps the plan ─────────────────────────────────


def test_non_empty_plan_returns_wrapped_content() -> None:
    """A non-empty plan must be wrapped with the header + footer and include the plan text."""
    import asyncio

    mod = _load_exit_plan_module()
    tool = mod.ExitPlanModeTool()
    plan_text = "1. Read file.\n2. Write patch.\n3. Run tests."
    call = ToolCall(id="c1", name="ExitPlanMode", arguments={"plan": plan_text})
    result = asyncio.run(tool.execute(call))
    assert result.is_error is False
    assert "Plan ready for review" in result.content
    assert plan_text in result.content
    assert "Awaiting user approval" in result.content


# ─── Test 3 — empty plan is an error ────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_plan_is_error(bad) -> None:
    """Empty / whitespace-only / missing plan must return is_error=True."""
    import asyncio

    mod = _load_exit_plan_module()
    tool = mod.ExitPlanModeTool()
    args = {"plan": bad} if bad is not None else {}
    call = ToolCall(id="c2", name="ExitPlanMode", arguments=args)
    result = asyncio.run(tool.execute(call))
    assert result.is_error is True
    assert "plan" in result.content.lower()


# ─── Test 4 — parallel_safe ─────────────────────────────────────────────


def test_parallel_safe_is_true() -> None:
    """ExitPlanMode is a pure transform — safe to run in parallel."""
    mod = _load_exit_plan_module()
    tool = mod.ExitPlanModeTool()
    assert tool.parallel_safe is True


# ─── Test 5 — coding-harness plugin register() wires ExitPlanMode ──────


def test_coding_harness_registers_exit_plan_mode(tmp_path: Path) -> None:
    """The real plugin loader must register ExitPlanMode in a fresh ToolRegistry.

    Uses the production ``load_plugin`` path with a PluginCandidate pointing
    at ``extensions/coding-harness``. This verifies end-to-end that the
    register() wiring works — not just that the file exists.
    """
    from opencomputer.agent.injection import InjectionEngine
    from opencomputer.hooks.engine import HookEngine
    from opencomputer.plugins.discovery import PluginCandidate, _parse_manifest
    from opencomputer.plugins.loader import PluginAPI, load_plugin
    from opencomputer.tools.registry import ToolRegistry

    harness_root = REPO_ROOT / "extensions" / "coding-harness"
    manifest_path = harness_root / "plugin.json"
    manifest = _parse_manifest(manifest_path)
    assert manifest is not None, "coding-harness plugin.json failed to parse"

    candidate = PluginCandidate(
        manifest=manifest,
        root_dir=harness_root,
        manifest_path=manifest_path,
    )

    tools = ToolRegistry()
    api = PluginAPI(
        tool_registry=tools,
        hook_engine=HookEngine(),
        provider_registry={},
        channel_registry={},
        injection_engine=InjectionEngine(),
        doctor_contributions=[],
        session_db_path=tmp_path / "session.db",
    )

    loaded = load_plugin(candidate, api)
    assert loaded is not None, "coding-harness plugin failed to load"
    assert "ExitPlanMode" in set(tools.names())
    tool = tools.get("ExitPlanMode")
    assert tool is not None
    assert tool.schema.name == "ExitPlanMode"
    assert tool.parallel_safe is True
