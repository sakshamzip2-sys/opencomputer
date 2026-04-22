"""
Coding harness plugin — register tools, modes, and hooks.

v2 layout (Phase 6c–6d):
    tools/       file and process tools + Rewind
    rewind/      content-hashed checkpoint store
    state/       session-scoped key/value store
    hooks/       PreToolUse auto-checkpoint, plan-mode block, post-edit review
    modes/       injection providers (coder-identity, plan, accept-edits, review)
    prompts/     Jinja2 templates backing the modes
    permissions/ scope checks — Phase 6e
    slash_commands/ in-chat controls — Phase 6f

Sibling imports use plain names because the plugin loader adds this dir to
sys.path + clears the common short-name module cache entries before each load.
"""

from __future__ import annotations

from pathlib import Path

# Sibling imports — loader-assisted (plugin dir on sys.path).
from context import HarnessContext  # type: ignore[import-not-found]
from hooks.auto_checkpoint import build_auto_checkpoint_hook_spec  # type: ignore[import-not-found]
from hooks.plan_block import build_plan_mode_hook_spec  # type: ignore[import-not-found]
from hooks.post_edit_review import build_post_edit_review_hook_spec  # type: ignore[import-not-found]
from modes.accept_edits_mode import AcceptEditsModeInjectionProvider  # type: ignore[import-not-found]
from modes.coder_identity import CoderIdentityInjectionProvider  # type: ignore[import-not-found]
from modes.plan_mode import PlanModeInjectionProvider  # type: ignore[import-not-found]
from modes.review_mode import ReviewModeInjectionProvider  # type: ignore[import-not-found]
from rewind.store import RewindStore  # type: ignore[import-not-found]
from state.store import SessionStateStore  # type: ignore[import-not-found]
from tools.background import (  # type: ignore[import-not-found]
    CheckOutputTool,
    KillProcessTool,
    StartProcessTool,
)
from tools.edit import EditTool  # type: ignore[import-not-found]
from tools.multi_edit import MultiEditTool  # type: ignore[import-not-found]
from tools.rewind import RewindTool  # type: ignore[import-not-found]
from tools.todo_write import TodoWriteTool  # type: ignore[import-not-found]

HARNESS_ROOT = Path.home() / ".opencomputer" / "harness"


def _build_context(api) -> HarnessContext:
    session_id = getattr(api, "session_id", None) or "default"
    workspace_root = getattr(api, "workspace_root", None) or Path.cwd()
    session_root = HARNESS_ROOT / session_id
    subagent_id = getattr(api, "subagent_id", None)
    rewind_store = RewindStore(
        session_root / "rewind",
        workspace_root=workspace_root,
        subagent_id=subagent_id,
    )
    session_state = SessionStateStore(session_root / "state")
    return HarnessContext(
        session_id=session_id,
        rewind_store=rewind_store,
        session_state=session_state,
    )


def register(api) -> None:  # PluginAPI duck-typed
    ctx = _build_context(api)

    # Tools
    api.register_tool(EditTool())
    api.register_tool(MultiEditTool())
    api.register_tool(TodoWriteTool())
    api.register_tool(StartProcessTool())
    api.register_tool(CheckOutputTool())
    api.register_tool(KillProcessTool())
    api.register_tool(RewindTool(ctx=ctx))

    # Modes — injection providers, ordered by priority.
    api.register_injection_provider(CoderIdentityInjectionProvider())  # priority 5
    api.register_injection_provider(PlanModeInjectionProvider())  # 10
    api.register_injection_provider(AcceptEditsModeInjectionProvider())  # 20
    api.register_injection_provider(ReviewModeInjectionProvider())  # 30

    # Hooks — enforcement + lifecycle interceptors.
    api.register_hook(build_plan_mode_hook_spec())  # hard-block in plan mode
    api.register_hook(build_auto_checkpoint_hook_spec(harness_ctx=ctx))
    api.register_hook(build_post_edit_review_hook_spec(harness_ctx=ctx))
