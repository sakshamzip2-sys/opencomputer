"""G3/G4 — shell-hook stdout JSON wire protocol (Hermes + Claude-Code shapes)."""

from __future__ import annotations

import asyncio
import stat

from opencomputer.agent.config import HookCommandConfig
from opencomputer.hooks.shell_handlers import make_shell_hook_handler
from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent


def _write_script(tmp_path, body: str):
    p = tmp_path / "hook.sh"
    p.write_text("#!/usr/bin/env bash\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _ctx(event: HookEvent = HookEvent.PRE_TOOL_USE) -> HookContext:
    return HookContext(
        event=event,
        session_id="sess-test",
        tool_call=ToolCall(id="t-1", name="Read", arguments={"path": "/tmp/x"}),
    )


# ─── G3 — both block shapes accepted ──────────────────────────────


def test_stdout_hermes_block_shape_blocks_with_message(tmp_path):
    """`{"action":"block","message":"why"}` on stdout → block."""
    script = _write_script(
        tmp_path,
        "cat - >/dev/null; printf '%s' '{\"action\":\"block\",\"message\":\"hermes block\"}'",
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "block"
    assert decision.reason == "hermes block"


def test_stdout_claude_code_block_shape_blocks_with_reason(tmp_path):
    """`{"decision":"block","reason":"why"}` on stdout → block."""
    script = _write_script(
        tmp_path,
        "cat - >/dev/null; printf '%s' '{\"decision\":\"block\",\"reason\":\"cc block\"}'",
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "block"
    assert decision.reason == "cc block"


def test_stdout_approve_shape_passes(tmp_path):
    """`{"action":"approve"}` → pass."""
    script = _write_script(
        tmp_path, "cat - >/dev/null; printf '%s' '{\"action\":\"approve\"}'"
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "pass"


def test_stdout_empty_object_passes(tmp_path):
    """`{}` → pass (Hermes idiomatic no-op)."""
    script = _write_script(tmp_path, "cat - >/dev/null; printf '%s' '{}'")
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "pass"


def test_stdout_malformed_json_falls_back_to_exit_code(tmp_path):
    """Invalid JSON on stdout + exit 2 + stderr → exit-code path wins."""
    script = _write_script(
        tmp_path,
        'cat - >/dev/null; echo "this is not json" >&1; '
        'echo "blocked by exit code" >&2; exit 2',
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "block"
    assert "blocked by exit code" in decision.reason


def test_stdout_block_wins_over_clean_exit_zero(tmp_path):
    """stdout JSON `block` + exit 0 → block wins (precedence rule)."""
    script = _write_script(
        tmp_path,
        "cat - >/dev/null; printf '%s' '{\"action\":\"block\",\"message\":\"json block\"}'; "
        "exit 0",
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "block"
    assert decision.reason == "json block"


def test_stdout_unrecognized_keys_pass(tmp_path):
    """JSON object with no recognised keys → pass."""
    script = _write_script(
        tmp_path,
        "cat - >/dev/null; printf '%s' '{\"some_other_key\":\"value\"}'",
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "pass"


# ─── G4 — context injection on PRE_LLM_CALL ────────────────────────


def test_stdout_context_injection_only_on_pre_llm_call(tmp_path):
    """`{"context":"..."}` on PRE_LLM_CALL → decision=pass, inject_context=text."""
    script = _write_script(
        tmp_path,
        "cat - >/dev/null; printf '%s' '{\"context\":\"Today is Friday\"}'",
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx(HookEvent.PRE_LLM_CALL)))
    assert decision is not None
    assert decision.decision == "pass"
    assert decision.inject_context == "Today is Friday"


def test_stdout_context_ignored_on_non_pre_llm_call(tmp_path):
    """`{"context":"..."}` on POST_TOOL_USE → no inject (just pass)."""
    script = _write_script(
        tmp_path, "cat - >/dev/null; printf '%s' '{\"context\":\"ignored\"}'"
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx(HookEvent.POST_TOOL_USE)))
    assert decision is not None
    assert decision.decision == "pass"
    assert decision.inject_context is None


def test_stdout_approve_plus_context_works_on_pre_llm_call(tmp_path):
    """{"action":"approve","context":"..."} on PRE_LLM_CALL → pass + inject."""
    script = _write_script(
        tmp_path,
        "cat - >/dev/null; printf '%s' "
        "'{\"action\":\"approve\",\"context\":\"branch=main\"}'",
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx(HookEvent.PRE_LLM_CALL)))
    assert decision is not None
    assert decision.decision == "pass"
    assert decision.inject_context == "branch=main"


# ─── G4 engine integration — collect_inject_contexts ────────────────


def test_collect_inject_contexts_runs_blocking_shell_hooks(tmp_path):
    """End-to-end: shell hook with fire_and_forget=False participates in collect."""
    from opencomputer.hooks.engine import engine
    from plugin_sdk.hooks import HookSpec

    script = _write_script(
        tmp_path,
        "cat - >/dev/null; printf '%s' '{\"context\":\"git: clean\"}'",
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )

    engine.unregister_all()
    engine.register(
        HookSpec(
            event=HookEvent.PRE_LLM_CALL,
            handler=handler,
            fire_and_forget=False,
        )
    )
    try:
        ctx = _ctx(HookEvent.PRE_LLM_CALL)
        contexts = asyncio.run(engine.collect_inject_contexts(ctx))
        assert contexts == ["git: clean"]
    finally:
        engine.unregister_all()


def test_collect_inject_contexts_skips_fire_and_forget_handlers(tmp_path):
    """Fire-and-forget handlers do NOT participate in collect (preserves
    existing PRE_LLM_CALL semantics for plugin hooks)."""
    from opencomputer.hooks.engine import engine
    from plugin_sdk.hooks import HookDecision, HookSpec

    async def slow_handler(ctx):
        return HookDecision(decision="pass", inject_context="should-not-appear")

    engine.unregister_all()
    engine.register(
        HookSpec(
            event=HookEvent.PRE_LLM_CALL,
            handler=slow_handler,
            fire_and_forget=True,  # default
        )
    )
    try:
        ctx = _ctx(HookEvent.PRE_LLM_CALL)
        contexts = asyncio.run(engine.collect_inject_contexts(ctx))
        assert contexts == []
    finally:
        engine.unregister_all()
