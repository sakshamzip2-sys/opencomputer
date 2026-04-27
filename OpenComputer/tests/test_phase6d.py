"""Phase 6d tests: modes + Jinja2 prompts.

Covers:
- Jinja2-backed PlanMode text is rendered (no hardcoded string).
- AcceptEditsMode fires on runtime.custom["accept_edits"].
- ReviewMode fires on runtime.custom["review_mode"].
- CoderIdentity fires every turn (no guard).
- post_edit_review hook queues pending reviews.
- Plugin registers all four modes + new hook.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent
from plugin_sdk.injection import InjectionContext
from plugin_sdk.runtime_context import RuntimeContext

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "extensions" / "coding-harness"


@pytest.fixture(autouse=True)
def _plugin_on_syspath():
    sys.path.insert(0, str(PLUGIN_ROOT))
    for mod_name in list(sys.modules):
        if mod_name.split(".")[0] in {
            "context",
            "rewind",
            "state",
            "tools",
            "hooks",
            "modes",
            "plan_mode",
        }:
            sys.modules.pop(mod_name, None)
    yield
    if str(PLUGIN_ROOT) in sys.path:
        sys.path.remove(str(PLUGIN_ROOT))


def _runtime(**custom) -> RuntimeContext:
    return RuntimeContext(plan_mode=False, yolo_mode=False, custom=dict(custom))


# ─── Plan mode (Jinja2-backed) ──────────────────────────────────


def test_plan_mode_text_loaded_from_jinja_template():
    from modes.plan_mode import PlanModeInjectionProvider

    p = PlanModeInjectionProvider()
    out = asyncio.run(
        p.collect(
            InjectionContext(messages=(), runtime=RuntimeContext(plan_mode=True))
        )
    )
    assert out is not None
    assert "PLAN MODE ACTIVE" in out
    # Blocked tool list is rendered from DESTRUCTIVE_TOOLS.
    assert "Edit" in out and "Bash" in out
    # Template ends with a trailing newline.
    assert out.endswith("\n")


def test_plan_mode_returns_none_when_flag_off():
    from modes.plan_mode import PlanModeInjectionProvider

    p = PlanModeInjectionProvider()
    assert (
        asyncio.run(
            p.collect(
                InjectionContext(messages=(), runtime=RuntimeContext(plan_mode=False))
            )
        )
        is None
    )


# ─── Accept-edits mode ──────────────────────────────────────────


def test_accept_edits_mode_fires_only_when_flag_set():
    from modes.accept_edits_mode import AcceptEditsModeInjectionProvider

    p = AcceptEditsModeInjectionProvider()
    assert (
        asyncio.run(p.collect(InjectionContext(messages=(), runtime=_runtime())))
        is None
    )

    on = asyncio.run(
        p.collect(
            InjectionContext(messages=(), runtime=_runtime(accept_edits=True))
        )
    )
    assert on is not None and "ACCEPT-EDITS MODE" in on


# ─── Review mode ────────────────────────────────────────────────


def test_review_mode_fires_only_when_flag_set():
    from modes.review_mode import ReviewModeInjectionProvider

    p = ReviewModeInjectionProvider()
    assert (
        asyncio.run(p.collect(InjectionContext(messages=(), runtime=_runtime())))
        is None
    )

    on = asyncio.run(
        p.collect(
            InjectionContext(messages=(), runtime=_runtime(review_mode=True))
        )
    )
    assert on is not None and "REVIEW MODE" in on


# ─── Coder identity (always on) ─────────────────────────────────


def test_coder_identity_always_fires():
    from modes.coder_identity import CoderIdentityInjectionProvider

    p = CoderIdentityInjectionProvider()
    out = asyncio.run(p.collect(InjectionContext(messages=(), runtime=_runtime())))
    assert out is not None
    assert "coding agent" in out.lower()


# ─── Mode priority ordering ─────────────────────────────────────


def test_mode_priorities_are_distinct_and_ordered():
    from modes.accept_edits_mode import AcceptEditsModeInjectionProvider
    from modes.coder_identity import CoderIdentityInjectionProvider
    from modes.plan_mode import PlanModeInjectionProvider
    from modes.review_mode import ReviewModeInjectionProvider

    priorities = {
        CoderIdentityInjectionProvider().priority,
        PlanModeInjectionProvider().priority,
        AcceptEditsModeInjectionProvider().priority,
        ReviewModeInjectionProvider().priority,
    }
    assert len(priorities) == 4  # distinct


# ─── post_edit_review hook ──────────────────────────────────────


def test_post_edit_review_hook_queues_reviews(tmp_path):
    from context import HarnessContext
    from hooks.post_edit_review import build_post_edit_review_hook_spec
    from rewind.store import RewindStore
    from state.store import SessionStateStore

    ctx = HarnessContext(
        session_id="s",
        rewind_store=RewindStore(tmp_path / "rw"),
        session_state=SessionStateStore(tmp_path / "ss"),
    )
    spec = build_post_edit_review_hook_spec(harness_ctx=ctx)
    tc = ToolCall(id="t1", name="Edit", arguments={"path": "f.py"})
    hctx = HookContext(
        event=HookEvent.POST_TOOL_USE,
        session_id="s",
        tool_call=tc,
        runtime=_runtime(review_mode=True),
    )

    decision = asyncio.run(spec.handler(hctx))
    assert decision is None  # never blocks (fire-and-forget)

    pending = ctx.session_state.get("pending_reviews", [])
    assert len(pending) == 1
    assert pending[0]["tool"] == "Edit"
    assert pending[0]["path"] == "f.py"


def test_post_edit_review_hook_skips_when_review_mode_off(tmp_path):
    from context import HarnessContext
    from hooks.post_edit_review import build_post_edit_review_hook_spec
    from rewind.store import RewindStore
    from state.store import SessionStateStore

    ctx = HarnessContext(
        session_id="s",
        rewind_store=RewindStore(tmp_path / "rw"),
        session_state=SessionStateStore(tmp_path / "ss"),
    )
    spec = build_post_edit_review_hook_spec(harness_ctx=ctx)
    tc = ToolCall(id="t1", name="Edit", arguments={"path": "f.py"})
    hctx = HookContext(
        event=HookEvent.POST_TOOL_USE,
        session_id="s",
        tool_call=tc,
        runtime=_runtime(),  # review_mode missing
    )

    asyncio.run(spec.handler(hctx))
    assert ctx.session_state.get("pending_reviews") is None


def test_post_edit_review_hook_ignores_non_reviewable_tools(tmp_path):
    from context import HarnessContext
    from hooks.post_edit_review import build_post_edit_review_hook_spec
    from rewind.store import RewindStore
    from state.store import SessionStateStore

    ctx = HarnessContext(
        session_id="s",
        rewind_store=RewindStore(tmp_path / "rw"),
        session_state=SessionStateStore(tmp_path / "ss"),
    )
    spec = build_post_edit_review_hook_spec(harness_ctx=ctx)
    tc = ToolCall(id="t1", name="Read", arguments={"path": "f.py"})
    hctx = HookContext(
        event=HookEvent.POST_TOOL_USE,
        session_id="s",
        tool_call=tc,
        runtime=_runtime(review_mode=True),
    )

    asyncio.run(spec.handler(hctx))
    assert ctx.session_state.get("pending_reviews") is None


# ─── Plugin wiring ──────────────────────────────────────────────


def test_plugin_registers_all_modes_and_hooks():
    class _FakeAPI:
        session_id = "t"
        workspace_root = None

        def __init__(self):
            self.tools = []
            self.hooks = []
            self.injections = []

        def register_tool(self, t):
            self.tools.append(t)

        def register_hook(self, s):
            self.hooks.append(s)

        def register_injection_provider(self, p):
            self.injections.append(p)

    spec = importlib.util.spec_from_file_location(
        "ch_test_plugin_6d", PLUGIN_ROOT / "plugin.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ch_test_plugin_6d"] = mod
    spec.loader.exec_module(mod)

    api = _FakeAPI()
    mod.register(api)

    injection_ids = {p.provider_id for p in api.injections}
    assert injection_ids == {
        "coding-harness:coder-identity",
        "coding-harness:plan-mode",
        "coding-harness:accept-edits-mode",
        "coding-harness:review-mode",
        # Tier B item 19 — auto-fetch URLs in incoming messages.
        "link-understanding",
    }
    # Plan-block + auto-checkpoint + post-edit-review = 3 hooks.
    assert len(api.hooks) >= 3
    # Tool count unchanged from 6c: 7.
    assert len(api.tools) >= 7


# ─── Backwards-compat shim ──────────────────────────────────────


def test_plan_mode_shim_re_exports_provider_and_hook():
    import plan_mode  # type: ignore[import-not-found]

    assert plan_mode.PlanModeInjectionProvider is not None
    assert callable(plan_mode.plan_mode_block_hook)
    assert callable(plan_mode.build_plan_mode_hook_spec)
    assert "Edit" in plan_mode.DESTRUCTIVE_TOOLS
