"""
Coding harness plugin — register tools, modes, and hooks.

v2 layout (Phase 6c–6e):
    tools/       file + process tools, Rewind, CheckpointDiff, RunTests
    rewind/      content-hashed checkpoint store
    state/       session-scoped key/value store
    hooks/       auto-checkpoint, plan-block, post-edit-review,
                 session-bootstrap, cleanup-session
    modes/       injection providers (coder-identity, plan, accept-edits, review)
    prompts/     Jinja2 templates backing the modes
    permissions/ scope checks + scope-check hook
    slash_commands/ in-chat controls — Phase 6f

Sibling imports use plain names because the plugin loader adds this dir to
sys.path + clears the common short-name module cache entries before each load.
"""

from __future__ import annotations

from pathlib import Path

# Sibling imports — loader-assisted (plugin dir on sys.path).
from context import HarnessContext  # type: ignore[import-not-found]
from hooks.auto_checkpoint import build_auto_checkpoint_hook_spec  # type: ignore[import-not-found]
from hooks.cleanup_session import build_cleanup_session_hook_spec  # type: ignore[import-not-found]
from hooks.plan_block import build_plan_mode_hook_spec  # type: ignore[import-not-found]
from hooks.post_edit_review import (
    build_post_edit_review_hook_spec,  # type: ignore[import-not-found]
)
from hooks.session_bootstrap import (
    build_session_bootstrap_hook_spec,  # type: ignore[import-not-found]
)
from modes.accept_edits_mode import (
    AcceptEditsModeInjectionProvider,  # type: ignore[import-not-found]
)
from modes.coder_identity import CoderIdentityInjectionProvider  # type: ignore[import-not-found]
from modes.plan_mode import PlanModeInjectionProvider  # type: ignore[import-not-found]
from modes.review_mode import ReviewModeInjectionProvider  # type: ignore[import-not-found]
from permissions.scope_check_hook import (
    build_scope_check_hook_spec,  # type: ignore[import-not-found]
)
from rewind.store import RewindStore  # type: ignore[import-not-found]
from slash_commands.accept_edits import AcceptEditsCommand  # type: ignore[import-not-found]
from slash_commands.checkpoint import CheckpointCommand  # type: ignore[import-not-found]
from slash_commands.diff import DiffCommand  # type: ignore[import-not-found]
from slash_commands.plan import PlanOffCommand, PlanOnCommand  # type: ignore[import-not-found]
from slash_commands.undo import UndoCommand  # type: ignore[import-not-found]
from state.store import SessionStateStore  # type: ignore[import-not-found]
from tools.background import (  # type: ignore[import-not-found]
    CheckOutputTool,
    KillProcessTool,
    StartProcessTool,
)
from tools.diff import CheckpointDiffTool  # type: ignore[import-not-found]
from tools.edit import EditTool  # type: ignore[import-not-found]
from tools.exit_plan_mode import ExitPlanModeTool  # type: ignore[import-not-found]
from tools.multi_edit import MultiEditTool  # type: ignore[import-not-found]
from tools.rewind import RewindTool  # type: ignore[import-not-found]
from tools.run_tests import RunTestsTool  # type: ignore[import-not-found]
from tools.todo_write import TodoWriteTool, set_default_db_path  # type: ignore[import-not-found]

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
    emit_fn = getattr(api, "emit_progress_fn", None)
    return HarnessContext(
        session_id=session_id,
        rewind_store=rewind_store,
        session_state=session_state,
        emit_progress_fn=emit_fn,
    )


def register(api) -> None:  # PluginAPI duck-typed
    ctx = _build_context(api)

    # Thread the per-profile session DB path into the TodoWrite module so
    # it doesn't need to import opencomputer.agent.config (SDK-boundary
    # violation). Falls back to the caller's default when the core doesn't
    # set session_db_path (legacy tests that hand-build a PluginAPI).
    session_db = getattr(api, "session_db_path", None)
    if session_db is not None:
        set_default_db_path(session_db)

    # Tools — 9 total (6 original + Rewind + CheckpointDiff + RunTests).
    api.register_tool(EditTool())
    api.register_tool(MultiEditTool())
    api.register_tool(TodoWriteTool(db_path=session_db) if session_db else TodoWriteTool())
    api.register_tool(ExitPlanModeTool())
    api.register_tool(StartProcessTool())
    api.register_tool(CheckOutputTool())
    api.register_tool(KillProcessTool())
    api.register_tool(RewindTool(ctx=ctx))
    api.register_tool(CheckpointDiffTool(ctx=ctx))
    api.register_tool(RunTestsTool(ctx=ctx))

    # Modes — 4 injection providers (priority 5 / 10 / 20 / 30).
    api.register_injection_provider(CoderIdentityInjectionProvider())
    api.register_injection_provider(PlanModeInjectionProvider())
    api.register_injection_provider(AcceptEditsModeInjectionProvider())
    api.register_injection_provider(ReviewModeInjectionProvider())

    # Tier B item 19 — auto-fetch URLs in incoming user messages so the
    # agent sees the article/page content inline alongside the message
    # without having to call WebFetch first. SSRF-guarded + cached
    # per-session. Toggleable via
    # ``opencomputer.agent.link_understanding.DEFAULT_CONFIG.enabled``.
    from opencomputer.agent.injection_providers import (
        LinkUnderstandingInjectionProvider,
    )
    api.register_injection_provider(LinkUnderstandingInjectionProvider())

    # Hooks — 7 total. Scope-check runs first (most deny-ey), then plan-block,
    # then auto-checkpoint, then post-edit-review. Session bootstrap / cleanup
    # are on their own events. The bg-notify subscriber listens on
    # Notification — it stashes a system message every time a background
    # process started via StartProcess exits so the agent loop can surface
    # the completion on its next turn (P-8).
    api.register_hook(build_scope_check_hook_spec())
    api.register_hook(build_plan_mode_hook_spec())
    api.register_hook(build_auto_checkpoint_hook_spec(harness_ctx=ctx))
    api.register_hook(build_post_edit_review_hook_spec(harness_ctx=ctx))
    api.register_hook(build_session_bootstrap_hook_spec(harness_ctx=ctx))
    api.register_hook(build_cleanup_session_hook_spec())

    # Round 2B P-8 — bg-process auto-notifications. Defensive import so a
    # missing core (extreme test fixtures) doesn't break harness load.
    try:
        from opencomputer.agent.bg_notify import build_default_subscriber_spec

        api.register_hook(build_default_subscriber_spec())
    except Exception:  # noqa: BLE001 — never break activation
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "bg-notify subscriber registration failed; auto-notifications disabled",
            exc_info=True,
        )

    # Slash commands — 6 total. Phase 12b6 Task D8 formalization. Only
    # register if the host's PluginAPI supports it — older cores without
    # ``register_slash_command`` still load the rest of the harness cleanly.
    if hasattr(api, "register_slash_command"):
        api.register_slash_command(PlanOnCommand(harness_ctx=ctx))
        api.register_slash_command(PlanOffCommand(harness_ctx=ctx))
        api.register_slash_command(AcceptEditsCommand(harness_ctx=ctx))
        api.register_slash_command(CheckpointCommand(harness_ctx=ctx))
        api.register_slash_command(DiffCommand(harness_ctx=ctx))
        api.register_slash_command(UndoCommand(harness_ctx=ctx))

    # Native introspection tools — Tier 1 only.
    #
    # The legacy Open-Interpreter subprocess wrapper was replaced in the
    # 2026-04-27 native-introspection migration. Tiers 2-5
    # had already been trimmed in the 2026-04-25 cleanup because each
    # overlapped with a feature OpenComputer already provides:
    #
    # * Tier 2 (email/SMS/Slack/Discord) → channel adapters + MCP.
    # * Tier 3 (browser) → built-in WebFetchTool covers raw fetches.
    # * Tier 4 (system control) → built-in BashTool.
    # * Tier 5 (schedule task / custom code) → ``opencomputer cron`` (G.1)
    #   + BashTool.
    #
    # What remains is Tier 1's unique value, now delivered via pure-pip
    # cross-platform implementations (psutil / mss / pyperclip /
    # rapidocr-onnxruntime): list apps, clipboard, screenshot, screen text,
    # recent files. F1 ConsentGate enforces capability claims at dispatch.
    #
    # The runtime alias for ``extensions.coding_harness`` is synthesized
    # below because the directory is named ``coding-harness`` (hyphen) and
    # cannot be imported as a dotted package natively. tests/conftest.py
    # registers the same alias for the test runner; production needs it
    # here before the introspection package import is attempted.
    try:
        import sys as _sys  # noqa: PLC0415
        import types as _types  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415

        if "extensions" not in _sys.modules:
            _ext_pkg = _types.ModuleType("extensions")
            _ext_pkg.__path__ = [str(_Path(__file__).resolve().parent.parent)]
            _sys.modules["extensions"] = _ext_pkg
        if "extensions.coding_harness" not in _sys.modules:
            _ch_pkg = _types.ModuleType("extensions.coding_harness")
            _ch_pkg.__path__ = [str(_Path(__file__).resolve().parent)]
            _ch_pkg.__package__ = "extensions.coding_harness"
            _sys.modules["extensions.coding_harness"] = _ch_pkg

        import logging as _logging  # noqa: PLC0415

        from extensions.coding_harness.introspection import (  # noqa: PLC0415
            ALL_TOOLS as _INTROSPECTION_TOOLS,
        )
        _log = _logging.getLogger("opencomputer.coding_harness.plugin")
        for _tool_cls in _INTROSPECTION_TOOLS:
            try:
                api.register_tool(_tool_cls())
            except Exception as _exc:  # noqa: BLE001
                _log.warning(
                    "Failed to register introspection tool %s: %s",
                    _tool_cls.__name__,
                    _exc,
                )
    except ImportError as _exc:
        import logging as _logging  # noqa: PLC0415
        _logging.getLogger("opencomputer.coding_harness.plugin").warning(
            "Introspection module not loadable: %s", _exc,
        )
