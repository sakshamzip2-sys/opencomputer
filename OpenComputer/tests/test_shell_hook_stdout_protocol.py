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


# ─── P6 — settings-hook registration uses fire_and_forget=False
# specifically for PRE_LLM_CALL so collect_inject_contexts picks it up.


def test_register_settings_hooks_pre_llm_call_uses_blocking(tmp_path, monkeypatch):
    """`_register_settings_hooks` registers PRE_LLM_CALL with
    fire_and_forget=False so collect_inject_contexts picks it up."""
    from opencomputer.agent.config import HookCommandConfig
    from opencomputer.cli import _register_settings_hooks
    from opencomputer.hooks.engine import engine
    from plugin_sdk.hooks import HookEvent

    # Build a minimal Config-like stub with one PRE_LLM_CALL hook and one
    # POST_TOOL_USE hook to verify the conditional.
    script = _write_script(
        tmp_path,
        "cat - >/dev/null; printf '%s' '{\"context\":\"smoke\"}'",
    )
    pre_hook = HookCommandConfig(
        event="PreLLMCall", command=str(script), timeout_seconds=5.0
    )
    post_hook = HookCommandConfig(
        event="PostToolUse", command=str(script), timeout_seconds=5.0
    )

    from types import SimpleNamespace
    cfg_stub = SimpleNamespace(hooks=(pre_hook, post_hook))

    engine.unregister_all()
    try:
        n = _register_settings_hooks(cfg_stub)
        assert n == 2
        # PRE_LLM_CALL spec must be fire_and_forget=False
        pre_specs = engine._ordered_specs(HookEvent.PRE_LLM_CALL)  # noqa: SLF001
        assert len(pre_specs) == 1
        assert pre_specs[0].fire_and_forget is False, (
            "PRE_LLM_CALL settings hooks must register with "
            "fire_and_forget=False so they participate in "
            "collect_inject_contexts"
        )
        # POST_TOOL_USE spec stays default (fire_and_forget=True)
        post_specs = engine._ordered_specs(HookEvent.POST_TOOL_USE)  # noqa: SLF001
        assert len(post_specs) == 1
        assert post_specs[0].fire_and_forget is True
    finally:
        engine.unregister_all()


def test_register_settings_hooks_skips_unknown_events(monkeypatch):
    """Unknown event names get logged + skipped, not raised."""
    from opencomputer.agent.config import HookCommandConfig
    from opencomputer.cli import _register_settings_hooks
    from opencomputer.hooks.engine import engine

    bogus = HookCommandConfig(
        event="ImpossibleEventName", command="/bin/true", timeout_seconds=1.0
    )
    valid = HookCommandConfig(
        event="PostToolUse", command="/bin/true", timeout_seconds=1.0
    )

    from types import SimpleNamespace
    cfg_stub = SimpleNamespace(hooks=(bogus, valid))

    engine.unregister_all()
    try:
        n = _register_settings_hooks(cfg_stub)
        # Bogus skipped, valid registered → count = 1
        assert n == 1
    finally:
        engine.unregister_all()


# ─── P7 — Production edge cases for stdout JSON parsing ────────────


def test_stdout_block_without_message_uses_default_reason(tmp_path):
    """`{"action":"block"}` without message → block with default reason."""
    script = _write_script(
        tmp_path, "cat - >/dev/null; printf '%s' '{\"action\":\"block\"}'"
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "block"
    assert decision.reason  # non-empty default
    assert "blocked" in decision.reason.lower() or "settings hook" in decision.reason.lower()


def test_stdout_non_string_context_ignored(tmp_path):
    """`{"context": 42}` (non-string) → no inject, just pass."""
    script = _write_script(
        tmp_path, "cat - >/dev/null; printf '%s' '{\"context\":42}'"
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx(HookEvent.PRE_LLM_CALL)))
    assert decision is not None
    assert decision.decision == "pass"
    assert decision.inject_context is None


def test_stdout_empty_string_context_ignored(tmp_path):
    """`{"context": "  "}` (whitespace-only) → no inject."""
    script = _write_script(
        tmp_path, "cat - >/dev/null; printf '%s' '{\"context\":\"   \"}'"
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx(HookEvent.PRE_LLM_CALL)))
    assert decision is not None
    assert decision.decision == "pass"
    assert decision.inject_context is None


def test_stdout_json_array_falls_back_to_exit_code(tmp_path):
    """Stdout is a JSON array (not object) → exit-code fallback."""
    script = _write_script(
        tmp_path, "cat - >/dev/null; printf '%s' '[\"not\", \"an\", \"object\"]'; exit 0"
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "pass"


def test_stdout_json_null_falls_back_to_exit_code(tmp_path):
    """Stdout is `null` → exit-code fallback."""
    script = _write_script(
        tmp_path, "cat - >/dev/null; printf '%s' 'null'; exit 0"
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "pass"


def test_stdout_non_string_action_passes(tmp_path):
    """`{"action": {"nested":"object"}}` → pass (recognised but not block-shaped)."""
    script = _write_script(
        tmp_path,
        "cat - >/dev/null; printf '%s' '{\"action\":{\"nested\":\"x\"}}'",
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "pass"


def test_stdout_block_wins_over_context_on_pre_llm_call(tmp_path):
    """Block + context combined on PRE_LLM_CALL → block wins; context dropped."""
    script = _write_script(
        tmp_path,
        "cat - >/dev/null; printf '%s' "
        "'{\"action\":\"block\",\"message\":\"nope\",\"context\":\"would-inject\"}'",
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx(HookEvent.PRE_LLM_CALL)))
    assert decision is not None
    assert decision.decision == "block"
    assert decision.reason == "nope"
    # inject_context not propagated when blocking — block is terminal
    assert decision.inject_context is None


def test_stdout_huge_json_object_handled(tmp_path):
    """Multi-MB stdout JSON object parses without DoS."""
    # Build a JSON object with a 1 MB context string (legal — large but
    # not pathological). Make sure the handler doesn't OOM or hang.
    big_context = "X" * (1024 * 1024)  # 1 MB
    script = tmp_path / "big.sh"
    payload = '{"context":"' + big_context + '"}'
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"cat - >/dev/null\n"
        f"cat <<'EOF'\n{payload}\nEOF\n"
    )
    import stat as _stat
    script.chmod(script.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP)

    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=10.0)
    )
    decision = asyncio.run(handler(_ctx(HookEvent.PRE_LLM_CALL)))
    assert decision is not None
    assert decision.decision == "pass"
    assert decision.inject_context is not None
    assert len(decision.inject_context) == 1024 * 1024


def test_stdout_truthy_non_recognized_keys_pass(tmp_path):
    """JSON with mix of unrecognised AND recognised-but-empty keys → pass."""
    script = _write_script(
        tmp_path,
        "cat - >/dev/null; printf '%s' '{\"foo\":\"bar\",\"action\":null}'",
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "pass"


def test_stdout_block_on_pre_llm_call_event(tmp_path):
    """Block decision works for PRE_LLM_CALL too (not just PRE_TOOL_USE).

    Hermes spec: block can apply to any pre-* event. Verify our parser
    doesn't gate block-shape on event type.
    """
    script = _write_script(
        tmp_path,
        "cat - >/dev/null; printf '%s' '{\"action\":\"block\",\"message\":\"refuse\"}'",
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx(HookEvent.PRE_LLM_CALL)))
    assert decision is not None
    assert decision.decision == "block"
    assert decision.reason == "refuse"


# ─── P8 — apply_inject_contexts (loop.py G4 mutation) ──────────────


def test_apply_inject_contexts_appends_to_last_user_message():
    """Last user msg gets injected text appended with double-newline separator."""
    from opencomputer.agent.loop import apply_inject_contexts
    from plugin_sdk.core import Message

    msgs = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi back"),
        Message(role="user", content="what time is it"),
    ]
    out = apply_inject_contexts(msgs, ["git: clean", "branch: main"])
    assert len(out) == 3
    assert out[0] == msgs[0]
    assert out[1] == msgs[1]
    assert out[2].role == "user"
    assert out[2].content == "what time is it\n\ngit: clean\n\nbranch: main"


def test_apply_inject_contexts_appends_new_message_after_assistant():
    """If last message is assistant, append a new user message instead."""
    from opencomputer.agent.loop import apply_inject_contexts
    from plugin_sdk.core import Message

    msgs = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi"),
    ]
    out = apply_inject_contexts(msgs, ["context line"])
    assert len(out) == 3
    assert out[2].role == "user"
    assert out[2].content == "context line"


def test_apply_inject_contexts_appends_new_message_after_tool_result():
    """If last message is a tool result, don't merge into it."""
    from opencomputer.agent.loop import apply_inject_contexts
    from plugin_sdk.core import Message

    msgs = [
        Message(role="user", content="check the weather"),
        Message(role="tool", content="cloudy", tool_call_id="t-1"),
    ]
    out = apply_inject_contexts(msgs, ["context"])
    assert len(out) == 3
    assert out[2].role == "user"
    assert out[2].content == "context"


def test_apply_inject_contexts_no_op_on_empty_contexts():
    """Empty contexts list → identity."""
    from opencomputer.agent.loop import apply_inject_contexts
    from plugin_sdk.core import Message

    msgs = [Message(role="user", content="hi")]
    out = apply_inject_contexts(msgs, [])
    assert out == msgs


def test_apply_inject_contexts_no_op_on_empty_messages():
    """Empty messages list → identity."""
    from opencomputer.agent.loop import apply_inject_contexts

    out = apply_inject_contexts([], ["context"])
    assert out == []


def test_apply_inject_contexts_handles_user_msg_with_tool_calls():
    """A user message that already has tool_calls (rare) gets a new
    trailing user message instead of merge — preserves linkage."""
    from opencomputer.agent.loop import apply_inject_contexts
    from plugin_sdk.core import Message, ToolCall

    msgs = [
        Message(
            role="user",
            content="use the tool",
            tool_calls=[ToolCall(id="t-1", name="Read", arguments={})],
        )
    ]
    out = apply_inject_contexts(msgs, ["ctx"])
    assert len(out) == 2
    # Original preserved (don't mangle tool_calls linkage)
    assert out[0].tool_calls is not None
    # New user appended
    assert out[1].role == "user"
    assert out[1].content == "ctx"


def test_apply_inject_contexts_joins_multiple_contexts_with_double_newline():
    """Multiple inject_context strings are joined with \\n\\n separator."""
    from opencomputer.agent.loop import apply_inject_contexts
    from plugin_sdk.core import Message

    msgs = [Message(role="user", content="q")]
    out = apply_inject_contexts(msgs, ["a", "b", "c"])
    assert out[0].content == "q\n\na\n\nb\n\nc"


def test_apply_inject_contexts_handles_empty_user_content():
    """Empty original content doesn't double-prefix the separator."""
    from opencomputer.agent.loop import apply_inject_contexts
    from plugin_sdk.core import Message

    msgs = [Message(role="user", content="")]
    out = apply_inject_contexts(msgs, ["ctx"])
    # No leading "\n\n"; just the context.
    assert out[0].content == "ctx"


def test_apply_inject_contexts_preserves_message_metadata():
    """Replaced last message keeps its non-content fields (attachments, etc.)."""
    from opencomputer.agent.loop import apply_inject_contexts
    from plugin_sdk.core import Message

    original = Message(role="user", content="q", attachments=["/tmp/img.png"])
    out = apply_inject_contexts([original], ["ctx"])
    assert out[0].attachments == ["/tmp/img.png"]
    assert out[0].content == "q\n\nctx"
