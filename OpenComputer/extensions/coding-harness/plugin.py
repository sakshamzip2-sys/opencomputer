"""
Coding harness plugin — register tools, injection provider, and hook.

Install this plugin to turn OpenComputer into a coding agent:
- Edit / MultiEdit / TodoWrite tools
- start_process / check_output / kill_process for dev servers
- Plan mode (via --plan flag) — refuses destructive tools when active
- Bundled code-reviewer skill (auto-activates on "review this PR" etc.)

Design (per Phase 6b plan):
- Flat file layout; plugin loader adds this dir to sys.path + clears
  common short-name module cache entries before each load.
- Sibling imports use plain names.
"""

from __future__ import annotations

try:
    from background import CheckOutputTool, KillProcessTool, StartProcessTool
    from edit import EditTool
    from multi_edit import MultiEditTool
    from plan_mode import PlanModeInjectionProvider, build_plan_mode_hook_spec
    from todo_write import TodoWriteTool
except ImportError:  # pragma: no cover — package-import fallback
    from extensions.coding_harness.background import (  # type: ignore
        CheckOutputTool,
        KillProcessTool,
        StartProcessTool,
    )
    from extensions.coding_harness.edit import EditTool  # type: ignore
    from extensions.coding_harness.multi_edit import MultiEditTool  # type: ignore
    from extensions.coding_harness.plan_mode import (  # type: ignore
        PlanModeInjectionProvider,
        build_plan_mode_hook_spec,
    )
    from extensions.coding_harness.todo_write import TodoWriteTool  # type: ignore


def register(api) -> None:  # PluginAPI duck-typed
    # Tools
    api.register_tool(EditTool())
    api.register_tool(MultiEditTool())
    api.register_tool(TodoWriteTool())
    api.register_tool(StartProcessTool())
    api.register_tool(CheckOutputTool())
    api.register_tool(KillProcessTool())

    # Plan-mode injection + hard-block hook (belt + suspenders)
    api.register_injection_provider(PlanModeInjectionProvider())
    api.register_hook(build_plan_mode_hook_spec())
