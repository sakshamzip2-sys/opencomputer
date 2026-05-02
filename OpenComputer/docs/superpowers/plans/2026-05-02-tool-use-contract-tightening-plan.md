# Tool-Use Contract Tightening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt 5 high-ROI items from the Anthropic tool-use contract (cache_control on tools, pause_turn/refusal handling, strict tool schemas, parallel-call nudge, memory tool routing audit) without disturbing OpenComputer's consent layer or memory architecture.

**Architecture:** Additive changes only. Touches `prompt_caching.py`, `loop.py`, `tool_contract.py`, `provider.py`, `base.j2`, and three tool description bodies. The legacy `apply_anthropic_cache_control` is preserved unchanged for backwards compatibility; a new `apply_full_cache_control` becomes the preferred entry point. `ToolSchema` gains a `strict` field (frozen-dataclass-safe via default).

**Tech Stack:** Python 3.13, Anthropic Python SDK (verified `ToolParam.strict` and `cache_control` fields exist), Jinja2, pytest.

**Spec:** `docs/superpowers/specs/2026-05-02-tool-use-contract-tightening-design.md`

**Branch:** `spec/tool-use-contract-tightening` (1 commit ahead of `origin/main`).

---

## File structure

| File | Change | Item |
|---|---|---|
| `opencomputer/agent/prompt_caching.py` | Add `apply_full_cache_control()` returning `(messages, tools)`; preserve legacy `apply_anthropic_cache_control()` | 1 |
| `extensions/anthropic-provider/provider.py` | Switch from `apply_anthropic_cache_control` to `apply_full_cache_control` at the two tools-passing call sites | 1 |
| `tests/test_prompt_caching.py` | Add 3 tests for the new function; existing tests untouched | 1 |
| `plugin_sdk/core.py` | Add `StopReason.PAUSE_TURN` and `StopReason.REFUSAL` to enum at line 417 | 2 |
| `opencomputer/agent/loop.py` | Extend `stop_reason_map` at line 2753; add pause_turn re-send (cap 3) and refusal exit | 2 |
| `tests/test_pause_refusal_stop_reasons.py` | NEW: 3 tests using the project's standard AgentLoop fixture pattern | 2 |
| `plugin_sdk/tool_contract.py` | Add `strict: bool = False` field to `ToolSchema` (frozen-safe via default); add `BaseTool.strict_mode` ClassVar; update `to_anthropic_format()` | 3 |
| `opencomputer/agent/loop.py` | Update tool-schema build site at line 2671 to set `strict` from each tool | 3 |
| `opencomputer/tools/*.py` | Per-tool audit: add `additionalProperties: False` where strict-compatible; opt-out (`strict_mode = False`) where not | 3 |
| `tests/test_tool_strict_mode.py` | NEW: parametrized strict-validation test + on-the-wire test | 3 |
| `opencomputer/agent/prompts/base.j2` | Insert `# Tool-call efficiency` section before `# Tone and style` | 4 |
| `tests/test_system_prompt_parallel_nudge.py` | NEW: assert nudge is in rendered prompt | 4 |
| `opencomputer/tools/memory_tool.py` | Rewrite Memory description with routing matrix | 5 |
| `opencomputer/tools/recall.py` | Rewrite Recall description with routing matrix | 5 |
| `opencomputer/tools/sessions.py` | Update SessionsList/History/Status descriptions | 5 |

---

## Task 1: cache_control on tools array

**Files:**
- Modify: `opencomputer/agent/prompt_caching.py`
- Modify: `extensions/anthropic-provider/provider.py` (the two tools-passing call sites)
- Test: `tests/test_prompt_caching.py`

**Approach.** Add ONE new function `apply_full_cache_control(messages, tools)` returning `(messages, tools)` tuple. It encapsulates the full 4-breakpoint allocation: 1 on `tools[-1]` + 1 on system + 2 on last non-system messages (when tools is non-empty), or 1 system + 3 messages (when no tools). Single entry point eliminates the two-call coordination footgun (a misaligned flag could push total to 5 breakpoints → API rejection). Existing `apply_anthropic_cache_control` is preserved unchanged for backwards compatibility.

- [ ] **Step 1.1: Write failing tests for `apply_full_cache_control`**

Add to `tests/test_prompt_caching.py`:

```python
def test_apply_full_cache_control_with_tools_marks_last_tool_and_3_message_breakpoints():
    """With tools: 1 tools[-1] + 1 system + 2 last non-system msgs = 4 total."""
    from opencomputer.agent.prompt_caching import apply_full_cache_control

    tools = [
        {"name": "Read", "description": "...", "input_schema": {}},
        {"name": "Write", "description": "...", "input_schema": {}},
        {"name": "Bash", "description": "...", "input_schema": {}},
    ]
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
        {"role": "user", "content": "msg3"},
    ]
    out_msgs, out_tools = apply_full_cache_control(msgs, tools)

    # Tools: only the last one marked
    assert "cache_control" not in out_tools[0]
    assert "cache_control" not in out_tools[1]
    assert out_tools[2]["cache_control"] == {"type": "ephemeral"}

    # Messages: system + last 2 non-system (msg2, msg3)
    msg_breakpoints = 0
    for m in out_msgs:
        c = m.get("content")
        if isinstance(c, list):
            msg_breakpoints += sum(1 for blk in c if isinstance(blk, dict) and "cache_control" in blk)
        if "cache_control" in m:
            msg_breakpoints += 1
    assert msg_breakpoints == 3

    # msg1 (oldest non-system) should NOT have cache_control
    msg1 = out_msgs[1]["content"]
    if isinstance(msg1, list):
        for blk in msg1:
            if isinstance(blk, dict):
                assert "cache_control" not in blk

    # Grand total ≤ 4 (Anthropic max)
    tools_breakpoints = sum(1 for t in out_tools if "cache_control" in t)
    assert msg_breakpoints + tools_breakpoints == 4


def test_apply_full_cache_control_no_tools_uses_4_message_breakpoints():
    """Empty/None tools → all 4 breakpoints on messages (system + last 3)."""
    from opencomputer.agent.prompt_caching import apply_full_cache_control

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "m1"},
        {"role": "assistant", "content": "m2"},
        {"role": "user", "content": "m3"},
        {"role": "assistant", "content": "m4"},
    ]
    out_msgs, out_tools = apply_full_cache_control(msgs, [])
    breakpoints = 0
    for m in out_msgs:
        c = m.get("content")
        if isinstance(c, list):
            breakpoints += sum(1 for blk in c if isinstance(blk, dict) and "cache_control" in blk)
        if "cache_control" in m:
            breakpoints += 1
    assert breakpoints == 4
    assert out_tools == []


def test_apply_full_cache_control_does_not_mutate_inputs():
    from opencomputer.agent.prompt_caching import apply_full_cache_control

    tools = [{"name": "Read"}]
    msgs = [{"role": "system", "content": "sys"}]
    apply_full_cache_control(msgs, tools)
    assert "cache_control" not in tools[0]
    assert msgs[0]["content"] == "sys"


def test_apply_full_cache_control_handles_none_tools():
    from opencomputer.agent.prompt_caching import apply_full_cache_control

    msgs = [{"role": "system", "content": "sys"}]
    out_msgs, out_tools = apply_full_cache_control(msgs, None)
    # Should behave identically to passing []
    assert out_tools == []
    sys_content = out_msgs[0]["content"]
    if isinstance(sys_content, list):
        assert any("cache_control" in blk for blk in sys_content if isinstance(blk, dict))
```

- [ ] **Step 1.2: Run tests to verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
.venv/bin/pytest tests/test_prompt_caching.py::test_apply_full_cache_control_with_tools_marks_last_tool_and_3_message_breakpoints -v
```

Expected: `ImportError: cannot import name 'apply_full_cache_control'`

- [ ] **Step 1.3: Add `apply_full_cache_control` (preserve existing function)**

In `opencomputer/agent/prompt_caching.py`, KEEP the existing `apply_anthropic_cache_control` and `_apply_cache_marker`. ADD `apply_full_cache_control` and one tiny helper. Final file (replace whole file):

```python
"""Anthropic prompt caching.

Reduces input token costs by caching the conversation prefix using up to 4
``cache_control`` breakpoints (Anthropic max).

Two entry points:

- ``apply_anthropic_cache_control(messages)``: legacy. Caches system +
  last 3 non-system messages. Used by callers that don't send tools.

- ``apply_full_cache_control(messages, tools)``: preferred. Returns
  ``(messages, tools)`` with breakpoints allocated as
  ``tools[-1] + system + last 2 non-system messages`` (4 total). Tool
  definitions (~8-30k tokens for ~40 tools) change rarely → highest
  cache hit rate. The deepest message breakpoint has the lowest hit
  rate (every turn changes the tail), so dropping it costs least.

Pure functions — no class state, no AIAgent dependency.
"""

import copy
from typing import Any


def _build_marker(cache_ttl: str) -> dict[str, Any]:
    marker: dict[str, Any] = {"type": "ephemeral"}
    if cache_ttl == "1h":
        marker["ttl"] = "1h"
    return marker


def _apply_cache_marker(msg: dict, cache_marker: dict, native_anthropic: bool = False) -> None:
    """Add cache_control to a single message, handling all format variations."""
    role = msg.get("role", "")
    content = msg.get("content")

    if role == "tool":
        if native_anthropic:
            msg["cache_control"] = cache_marker
        return

    if content is None or content == "":
        msg["cache_control"] = cache_marker
        return

    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": cache_marker}
        ]
        return

    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = cache_marker


def _cache_tail_messages(
    messages: list[dict[str, Any]],
    n_tail: int,
    marker: dict[str, Any],
    native_anthropic: bool,
) -> None:
    """Mark up to n_tail non-system messages from the end."""
    non_sys = [i for i in range(len(messages)) if messages[i].get("role") != "system"]
    for idx in non_sys[-n_tail:]:
        _apply_cache_marker(messages[idx], marker, native_anthropic=native_anthropic)


def apply_anthropic_cache_control(
    api_messages: list[dict[str, Any]],
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
) -> list[dict[str, Any]]:
    """Legacy: 4 breakpoints on messages only (system + last 3).

    Preserved for backwards compatibility. Prefer ``apply_full_cache_control``
    when sending tools.
    """
    messages = copy.deepcopy(api_messages)
    if not messages:
        return messages

    marker = _build_marker(cache_ttl)
    breakpoints_used = 0

    if messages[0].get("role") == "system":
        _apply_cache_marker(messages[0], marker, native_anthropic=native_anthropic)
        breakpoints_used += 1

    _cache_tail_messages(messages, 4 - breakpoints_used, marker, native_anthropic)
    return messages


def apply_full_cache_control(
    api_messages: list[dict[str, Any]],
    api_tools: list[dict[str, Any]] | None,
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply 4-breakpoint strategy across messages AND tools array.

    With tools (non-empty):  tools[-1] + system + last 2 non-system msgs (4 total)
    Without tools:           system + last 3 non-system msgs            (4 total)

    Returns ``(messages, tools)`` — both deep-copied. ``api_tools=None`` is
    treated as ``[]``. Inputs are not mutated.
    """
    messages = copy.deepcopy(api_messages)
    tools = copy.deepcopy(api_tools) if api_tools else []

    if not messages and not tools:
        return messages, tools

    marker = _build_marker(cache_ttl)

    # Tools breakpoint (1 of 4)
    tools_used = 0
    if tools:
        tools[-1]["cache_control"] = marker
        tools_used = 1

    # System breakpoint
    sys_used = 0
    if messages and messages[0].get("role") == "system":
        _apply_cache_marker(messages[0], marker, native_anthropic=native_anthropic)
        sys_used = 1

    # Tail-messages breakpoints filling remaining budget
    remaining = 4 - tools_used - sys_used
    if remaining > 0 and messages:
        _cache_tail_messages(messages, remaining, marker, native_anthropic)

    return messages, tools
```

- [ ] **Step 1.4: Run new tests to verify they pass**

```bash
.venv/bin/pytest tests/test_prompt_caching.py -v
```

Expected: all 4 new tests PASS, all 6 existing tests PASS (untouched).

- [ ] **Step 1.5: Switch the Anthropic provider to `apply_full_cache_control`**

In `extensions/anthropic-provider/provider.py`:

1. Find every import line for `apply_anthropic_cache_control`. If imported, leave it (some non-tools paths may still use it). Add an import for `apply_full_cache_control`:

```python
from opencomputer.agent.prompt_caching import (
    apply_anthropic_cache_control,
    apply_full_cache_control,
)
```

2. Find the existing call to `apply_anthropic_cache_control` (search for it in the file — it's the only place that mutates messages with cache markers, around line 518). Replace the call with `apply_full_cache_control` and pass tools alongside:

Before:
```python
api_messages = apply_anthropic_cache_control(
    anthropic_messages,
    cache_ttl=cache_ttl,
    native_anthropic=True,
)
```

After:
```python
api_tools = [t.to_anthropic_format() for t in (tools or [])]
api_messages, api_tools = apply_full_cache_control(
    anthropic_messages,
    api_tools,
    cache_ttl=cache_ttl,
    native_anthropic=True,
)
```

3. Remove or update the older `kwargs["tools"] = [t.to_anthropic_format() for t in tools]` lines at the two old call sites (~437, ~528). They're now built and cached above; assign directly:

```python
if api_tools:
    kwargs["tools"] = api_tools
```

**Important.** Read the full file context before editing to confirm the variable names (`anthropic_messages`, `cache_ttl`, etc.) match exactly. Don't edit blind.

- [ ] **Step 1.6: Run anthropic-provider tests**

```bash
.venv/bin/pytest tests/test_prompt_caching.py extensions/anthropic-provider/ -v 2>&1 | tail -20
.venv/bin/pytest tests/ -k "anthropic_provider or cache" -v 2>&1 | tail -20
```

Expected: all PASS.

- [ ] **Step 1.7: Commit**

```bash
git add opencomputer/agent/prompt_caching.py extensions/anthropic-provider/provider.py tests/test_prompt_caching.py
git commit -m "$(cat <<'EOF'
feat(cache): apply cache_control to tools array (Item 1)

Add apply_full_cache_control(messages, tools) -> (messages, tools)
returning the 4-breakpoint allocation tools[-1] + system + last 2
messages (when tools non-empty) or system + last 3 (when empty).

Tool definitions (~8-30k tokens for ~40 tools) now cache; the lowest-
hit message breakpoint is dropped. Single entry point prevents the
coordination footgun where mismatched flags hit 5 breakpoints and the
API rejects.

Legacy apply_anthropic_cache_control preserved unchanged for backwards
compat; provider switched to apply_full_cache_control.

Spec: docs/superpowers/specs/2026-05-02-tool-use-contract-tightening-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: pause_turn and refusal stop reasons

**Files:**
- Modify: `plugin_sdk/core.py:417` (StopReason enum)
- Modify: `opencomputer/agent/loop.py:2753-2764` (stop_reason_map and downstream handling)
- Test: `tests/test_pause_refusal_stop_reasons.py` (NEW)

**Approach.** Add two enum values. Map `pause_turn` to a new `StopReason.PAUSE_TURN`; loop re-issues the provider call with the conversation including the paused assistant response, capped at 3 consecutive pauses. Map `refusal` to `StopReason.REFUSAL`; loop exits without retry, surfacing the assistant text.

The test fixture follows the pattern in `tests/test_loop_emits_bus_events.py` — build a real `AgentLoop` with `Config`, `LoopConfig`, `SessionDB`, `ToolRegistry`, and a `BaseProvider` mock. This is the project's established pattern; do not invent a new fixture.

- [ ] **Step 2.1: Add enum values**

In `plugin_sdk/core.py:417`, replace the `StopReason` class body:

```python
class StopReason(str, Enum):
    """Why a conversation step ended."""

    END_TURN = "end_turn"  # model produced final response, no more tool calls
    TOOL_USE = "tool_use"  # model wants to call tools — loop continues
    MAX_TOKENS = "max_tokens"  # hit output limit
    INTERRUPTED = "interrupted"  # user cancelled
    BUDGET_EXHAUSTED = "budget_exhausted"  # iteration budget spent
    ERROR = "error"  # unrecoverable error
    PAUSE_TURN = "pause_turn"  # server-tool work paused; re-send to continue (cap 3)
    REFUSAL = "refusal"  # model refused; surface as final, do not retry
```

- [ ] **Step 2.2: Write failing tests using the project's standard fixture**

Create `tests/test_pause_refusal_stop_reasons.py`:

```python
"""Tests for pause_turn and refusal stop_reason handling in the agent loop.

Fixture pattern adapted from tests/test_loop_emits_bus_events.py — build a
real AgentLoop against a mock provider.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from opencomputer.agent.config import Config, LoopConfig
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.state import SessionDB
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import Message, StopReason
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage


class _ScriptedProvider(BaseProvider):
    """Provider that returns a pre-scripted sequence of responses."""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def complete(self, **_kwargs: Any) -> ProviderResponse:
        if not self._responses:
            raise AssertionError("scripted provider exhausted")
        self.calls += 1
        return self._responses.pop(0)

    async def stream_complete(self, **kwargs: Any):
        # Not used by these tests
        raise NotImplementedError


def _resp(stop_reason: str, text: str = "") -> ProviderResponse:
    return ProviderResponse(
        message=Message(role="assistant", content=text),
        stop_reason=stop_reason,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def _build_loop(provider: BaseProvider, tmp_path) -> AgentLoop:
    """Construct a minimal AgentLoop wired to the scripted provider."""
    config = Config(loop=LoopConfig(max_iterations=10))
    session_db = SessionDB(db_path=tmp_path / "test.db")
    registry = ToolRegistry()
    return AgentLoop(
        provider=provider,
        config=config,
        session_db=session_db,
        tool_registry=registry,
    )


@pytest.mark.asyncio
async def test_pause_turn_then_end_turn_continues_loop(tmp_path):
    """pause_turn → re-send → end_turn yields final answer in 2 calls."""
    provider = _ScriptedProvider([
        _resp("pause_turn", "(paused)"),
        _resp("end_turn", "Final answer."),
    ])
    loop = _build_loop(provider, tmp_path)
    result = await loop.run_conversation("test query")

    assert provider.calls == 2
    assert "Final answer" in (result.final_text or "")


@pytest.mark.asyncio
async def test_pause_turn_cap_exceeded_exits_with_warning(tmp_path, caplog):
    """4 consecutive pause_turn → loop exits at cap (≤4 calls), warning logged."""
    provider = _ScriptedProvider([_resp("pause_turn", f"paused {i}") for i in range(5)])
    loop = _build_loop(provider, tmp_path)

    with caplog.at_level("WARNING"):
        await loop.run_conversation("test query")

    assert provider.calls <= 4  # 1 initial + 3 re-sends max
    assert any("pause_turn" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_refusal_exits_without_retry(tmp_path):
    """refusal → loop exits in 1 call, surfaces the assistant text."""
    provider = _ScriptedProvider([_resp("refusal", "I can't help with that.")])
    loop = _build_loop(provider, tmp_path)
    result = await loop.run_conversation("dangerous query")

    assert provider.calls == 1
    assert "can't help" in (result.final_text or "")
```

**Note.** The `Config(loop=LoopConfig(max_iterations=10))` and `AgentLoop(...)` constructor signatures may have additional required args in this codebase. Before committing, verify by reading `tests/test_loop_emits_bus_events.py` and copy whatever extra construction it does (e.g. `injection_engine`, `compaction_engine`, hook engine wiring). Do NOT proceed with placeholder construction.

- [ ] **Step 2.3: Verify fixture by adapting from the reference test**

```bash
head -120 tests/test_loop_emits_bus_events.py
```

Read the actual `AgentLoop(...)` construction the reference test uses. Apply the same construction in `_build_loop`. If the reference test has shared helpers in `conftest.py` or `tests/_helpers.py`, import them rather than duplicating.

- [ ] **Step 2.4: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_pause_refusal_stop_reasons.py -v
```

Expected: 3 FAIL — assertion errors because the loop doesn't yet handle pause_turn/refusal.

- [ ] **Step 2.5: Extend the stop_reason_map**

In `opencomputer/agent/loop.py`, find lines 2753-2764 and replace:

```python
        stop_reason_map = {
            "end_turn": StopReason.END_TURN,
            "tool_use": StopReason.TOOL_USE,
            "max_tokens": StopReason.MAX_TOKENS,
            "stop_sequence": StopReason.END_TURN,
            "pause_turn": StopReason.PAUSE_TURN,
            "refusal": StopReason.REFUSAL,
        }
        stop = stop_reason_map.get(resp.stop_reason, StopReason.END_TURN)

        if resp.message.tool_calls and stop == StopReason.END_TURN:
            stop = StopReason.TOOL_USE
```

- [ ] **Step 2.6: Read the loop body to find the right insertion point for pause/refusal handling**

Read around line 1093-1700 (the main `for _iter in range(...)` loop) to understand:

1. Where `step.should_continue` is checked
2. How the loop exits via `return ConversationResult(...)`
3. Where `messages` is mutated between turns
4. Whether `step` is mutable, NamedTuple (has `_replace`), or frozen dataclass (use `dataclasses.replace`)

```bash
sed -n '1080,1180p' opencomputer/agent/loop.py
sed -n '1610,1690p' opencomputer/agent/loop.py
cat opencomputer/agent/step.py
```

- [ ] **Step 2.7: Insert pause/refusal handling**

After reading the loop body, insert handling AT THE TOP of the per-iteration block, immediately after `step = await self._run_one_step(...)` returns. Use the appropriate `replace` pattern based on what `step.py` shows:

```python
            # 2026-05-02 (Item 2): handle pause_turn and refusal stop reasons.
            #
            # pause_turn: server-tool work paused mid-call. Per Anthropic
            # contract, re-send the conversation (including the paused
            # assistant response) to continue. Cap at 3 consecutive pauses
            # to avoid pathological loops on broken server tools.
            #
            # refusal: model declined. Exit immediately, do NOT retry.

            if step.stop_reason == StopReason.PAUSE_TURN:
                self._pause_turn_count = getattr(self, "_pause_turn_count", 0) + 1
                if self._pause_turn_count >= 3:
                    import logging
                    logging.getLogger(__name__).warning(
                        "pause_turn cap (3) exceeded — forcing END_TURN. "
                        "A server tool may be stuck in a re-send loop."
                    )
                    self._pause_turn_count = 0
                    # Treat as END_TURN. Append paused content as the final
                    # assistant message and exit cleanly.
                    if step.assistant_message is not None:
                        messages.append(step.assistant_message)
                    break
                # Below cap: append paused content and continue the loop
                # so the next iteration re-sends.
                if step.assistant_message is not None:
                    messages.append(step.assistant_message)
                continue

            if step.stop_reason == StopReason.REFUSAL:
                # Exit immediately. Final assistant text is in
                # step.assistant_message.content.
                self._pause_turn_count = 0
                if step.assistant_message is not None:
                    messages.append(step.assistant_message)
                break

            # Reset pause counter on any other outcome
            self._pause_turn_count = 0
```

**Where to put `_pause_turn_count`.** The use of `getattr(self, "_pause_turn_count", 0)` allows lazy initialization without modifying `AgentLoop.__init__`. If the codebase prefers explicit init, add `self._pause_turn_count = 0` to `__init__`.

**Where to insert in the loop.** This block goes AFTER the `_run_one_step` call but BEFORE the `if not step.should_continue:` check that returns the final result. The intent is: pause/refusal are handled BEFORE the normal end-of-turn logic.

- [ ] **Step 2.8: Run tests**

```bash
.venv/bin/pytest tests/test_pause_refusal_stop_reasons.py -v
```

Expected: 3 PASS.

- [ ] **Step 2.9: Run loop test suite for regressions**

```bash
.venv/bin/pytest tests/ -k "loop or stop_reason or agent_loop" -v 2>&1 | tail -30
```

Expected: all PASS.

- [ ] **Step 2.10: Commit**

```bash
git add plugin_sdk/core.py opencomputer/agent/loop.py tests/test_pause_refusal_stop_reasons.py
git commit -m "$(cat <<'EOF'
feat(loop): handle pause_turn and refusal stop reasons (Item 2)

pause_turn was silently falling through to END_TURN, truncating
server-tool work. refusal was hidden as a generic end. Both now
map to dedicated StopReason values with explicit handling:
- pause_turn: re-send conversation (cap 3 consecutive)
- refusal: surface assistant text, exit without retry

Latent today (no server tools enabled) but critical the moment any are.

Spec: docs/superpowers/specs/2026-05-02-tool-use-contract-tightening-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: strict mode on tool definitions

**Files:**
- Modify: `plugin_sdk/tool_contract.py`
- Modify: `opencomputer/agent/loop.py:2671` (where `tool_schemas` is built)
- Modify: `opencomputer/tools/*.py` (per-tool audit)
- Test: `tests/test_tool_strict_mode.py` (NEW)

**Approach.** Add `strict: bool = False` field directly to the frozen `ToolSchema` dataclass (defaulting False preserves backwards-compat with all existing constructors). Add `BaseTool.strict_mode: ClassVar[bool] = True` defaulting True. The loop's `_filtered_schemas()` is augmented to set `strict` on each `ToolSchema` based on the tool's `strict_mode`. `to_anthropic_format()` emits `"strict": True` when the field is True.

This avoids the frozen-dataclass attribute-mutation footgun: the `strict` field exists on the dataclass itself.

**Strict requirements per Anthropic SDK:** "When true, guarantees schema validation on tool names and inputs." Object schemas should declare `additionalProperties: false`, all required fields listed, no implicit type unions.

- [ ] **Step 3.1: Add `strict` field to `ToolSchema` and `strict_mode` to `BaseTool`**

Replace the entire contents of `plugin_sdk/tool_contract.py`:

```python
"""
Tool contract — what plugin authors implement to add a new tool.

A tool is any callable the agent can invoke: Read, Write, Bash, etc.
Plugins can add new ones by subclassing `BaseTool` and registering
via `register_plugin(..., tools=[MyTool])`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from plugin_sdk.consent import CapabilityClaim
from plugin_sdk.core import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """OpenAI-compatible JSON schema for a tool."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object

    #: 2026-05-02 (Item 3): when True, ``to_anthropic_format`` emits
    #: ``"strict": True`` so Anthropic enforces schema validation on
    #: tool names and inputs. Defaults False for backwards-compat —
    #: every existing ToolSchema(...) constructor call still works.
    #: The agent loop sets this from the tool's ``BaseTool.strict_mode``
    #: when building the request.
    strict: bool = False

    def to_openai_format(self) -> dict[str, Any]:
        """Convert to the dict format the OpenAI API expects."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic_format(self) -> dict[str, Any]:
        """Convert to the dict format the Anthropic API expects."""
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }
        if self.strict:
            out["strict"] = True
        return out


class BaseTool(ABC):
    """Base class for a tool. Subclass and implement `schema` + `execute`."""

    #: Whether this tool is safe to run in parallel with other parallel-safe tools.
    parallel_safe: bool = False

    #: Maximum size of the result string (longer is truncated with a notice).
    max_result_size: int = 100_000

    #: F1 (Sub-project F): capabilities this tool needs the user to have
    #: granted. Empty list (default) means unprivileged — no gate check.
    #: Subclasses SHOULD override with a tuple (not list) to avoid the
    #: mutable-default-class-attribute footgun.
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = ()

    #: 2026-05-02 (Item 3): when True (default), the tool's schema is
    #: sent with ``strict: true`` to Anthropic. Tools whose schemas
    #: cannot satisfy strict requirements (free-form params, polymorphic
    #: args, missing ``additionalProperties: false``) override to False
    #: with a one-line comment explaining why.
    strict_mode: ClassVar[bool] = True

    @property
    @abstractmethod
    def schema(self) -> ToolSchema:
        """Return the JSON schema describing this tool's input."""
        ...

    @abstractmethod
    async def execute(self, call: ToolCall) -> ToolResult:
        """Actually run the tool. Must handle its own errors — never raise."""
        ...


__all__ = ["ToolSchema", "BaseTool"]
```

- [ ] **Step 3.2: Update the loop to set `strict` on each ToolSchema before passing to provider**

In `opencomputer/agent/loop.py`, find `_filtered_schemas` (line 2619) and the call to `sort_tools_for_request` (line 2671). The schemas currently come straight from `tool.schema`. Wrap the build to copy each schema with the tool's `strict_mode`.

Read the existing `_filtered_schemas` first:

```bash
sed -n '2615,2680p' opencomputer/agent/loop.py
```

Then update — replace the line where `tool_schemas` is built:

Before (around line 2671):
```python
tool_schemas = sort_tools_for_request(self._filtered_schemas())
```

After:
```python
import dataclasses as _dc

# 2026-05-02 (Item 3): propagate each tool's strict_mode into the
# schema sent to the provider. ToolSchema is frozen; use replace().
def _attach_strict(schemas):
    for s in schemas:
        # Find the originating tool by name; if registry can't resolve
        # (e.g. ad-hoc schema), default to non-strict for safety.
        tool = self._tool_registry.get_tool(s.name) if hasattr(self, "_tool_registry") else None
        strict_mode = getattr(tool, "strict_mode", False) if tool else False
        yield _dc.replace(s, strict=strict_mode) if strict_mode and not s.strict else s

tool_schemas = sort_tools_for_request(list(_attach_strict(self._filtered_schemas())))
```

**Important.** The exact registry lookup (`self._tool_registry.get_tool(...)`) is a guess. Read `opencomputer/tools/registry.py` to confirm the right method:

```bash
grep -n "def get\|def lookup\|def find\|class ToolRegistry" opencomputer/tools/registry.py
```

Use whatever method exists. If no by-name lookup exists, add one (3 LOC). If the registry stores tool instances keyed by name already, use direct dict access.

- [ ] **Step 3.3: Write failing parametrized strict-validation test**

Create `tests/test_tool_strict_mode.py`:

```python
"""Validate that tools declared strict_mode=True have schemas that satisfy
Anthropic's strict-mode contract (additionalProperties: false, explicit types)."""
from __future__ import annotations

import pytest

from plugin_sdk.tool_contract import BaseTool, ToolSchema


def _is_strict_compatible(schema_params: dict) -> tuple[bool, str]:
    """Return (passes, reason) for a JSON Schema object dict."""
    if schema_params.get("type") != "object":
        return False, "top-level type is not 'object'"
    if schema_params.get("additionalProperties") is not False:
        return False, "additionalProperties is not False"
    props = schema_params.get("properties") or {}
    for name, spec in props.items():
        if not isinstance(spec, dict):
            return False, f"property '{name}' spec is not a dict"
        if "type" not in spec and "enum" not in spec and "$ref" not in spec:
            return False, f"property '{name}' has no type/enum/$ref"
    return True, ""


def _all_registered_tools() -> list[BaseTool]:
    """Build the same tool registry the agent uses at startup.

    Adapt the import below to whatever the project actually uses to build
    the default registry. Common candidates:
      - opencomputer.tools.registry.build_default_registry()
      - opencomputer.cli._build_tool_registry()
      - manual instantiation of each tool class
    """
    from opencomputer.tools.registry import ToolRegistry

    # If a build_default_registry helper exists, prefer it.
    try:
        from opencomputer.tools.registry import build_default_registry
        reg = build_default_registry()
    except ImportError:
        # Fallback: build registry by importing every tool module and
        # registering each BaseTool subclass found.
        reg = ToolRegistry()
        import importlib
        import pkgutil
        import opencomputer.tools as tools_pkg

        for finder, name, ispkg in pkgutil.iter_modules(tools_pkg.__path__):
            if name.startswith("_") or name == "registry":
                continue
            mod = importlib.import_module(f"opencomputer.tools.{name}")
            for attr in dir(mod):
                obj = getattr(mod, attr)
                if (
                    isinstance(obj, type)
                    and issubclass(obj, BaseTool)
                    and obj is not BaseTool
                ):
                    try:
                        reg.register(obj())
                    except Exception:
                        pass  # tool may have non-trivial __init__; skip
    return list(reg.iter_tools())


_TOOLS = _all_registered_tools()


@pytest.mark.parametrize("tool", _TOOLS, ids=lambda t: t.schema.name)
def test_strict_tools_have_compatible_schemas(tool: BaseTool):
    """Every tool with strict_mode=True must have a strict-compatible schema."""
    if not getattr(tool, "strict_mode", False):
        pytest.skip(f"{tool.schema.name} opted out of strict mode")
    passes, reason = _is_strict_compatible(tool.schema.parameters)
    assert passes, f"{tool.schema.name} not strict-compatible: {reason}"


def test_at_least_80_percent_of_tools_are_strict():
    """≥80% strict adoption is the spec's acceptance bar."""
    if not _TOOLS:
        pytest.skip("no tools registered")
    strict_count = sum(1 for t in _TOOLS if getattr(t, "strict_mode", False))
    pct = strict_count / len(_TOOLS)
    assert pct >= 0.80, (
        f"only {strict_count}/{len(_TOOLS)} ({pct:.0%}) tools are strict; "
        f"spec requires ≥80%"
    )


def test_strict_emitted_in_anthropic_format():
    """ToolSchema with strict=True includes the strict field in API format."""
    s_no = ToolSchema(
        name="DummyA", description="x",
        parameters={"type": "object", "additionalProperties": False, "properties": {}},
    )
    s_yes = ToolSchema(
        name="DummyB", description="x",
        parameters={"type": "object", "additionalProperties": False, "properties": {}},
        strict=True,
    )
    assert "strict" not in s_no.to_anthropic_format()
    assert s_yes.to_anthropic_format()["strict"] is True
```

**Note.** The fallback registry-build path has a try/except — if it doesn't work cleanly, find the project's actual registry-build helper (`grep -rn "build_default_registry\|build.*registry" opencomputer/` will reveal it) and adapt the import accordingly.

- [ ] **Step 3.4: Run test — expect mixed PASS/FAIL/SKIP**

```bash
.venv/bin/pytest tests/test_tool_strict_mode.py -v 2>&1 | tee /tmp/strict_audit.log
```

Expected: a mix. Each FAIL identifies a tool whose schema needs adjustment OR a tool that should opt out.

- [ ] **Step 3.5: Per-tool audit — for each FAIL, decide:**

For every failing tool listed in `/tmp/strict_audit.log`:

(a) **Fix the schema** (preferred): edit the tool's source file. Add `additionalProperties: false`, ensure every property has explicit `type`/`enum`/`$ref`:

```python
parameters={
    "type": "object",
    "additionalProperties": False,  # ADD
    "properties": {
        "command": {"type": "string", "description": "..."},
    },
    "required": ["command"],
}
```

(b) **Opt out** if the parameter is genuinely free-form (e.g. arbitrary shell command):

```python
class BashTool(BaseTool):
    parallel_safe = False
    strict_mode = False  # 2026-05-02: free-form shell command — strict adds friction with no model-quality win

    @property
    def schema(self) -> ToolSchema: ...
```

The 80% strict bar is the acceptance gate. If audit results land below 80%, prefer fixing schemas over opting out.

- [ ] **Step 3.6: Re-run test — expect all PASS or SKIP**

```bash
.venv/bin/pytest tests/test_tool_strict_mode.py -v
```

Expected: every parametrized case PASS or SKIP. The 80% threshold test PASSES.

- [ ] **Step 3.7: Run full pytest suite to catch any test that asserts an exact tool schema dict**

```bash
.venv/bin/pytest tests/ 2>&1 | tail -30
```

Expected: all PASS. If any test asserts exact schema dict content and that dict gained `additionalProperties: False`, update the assertion.

- [ ] **Step 3.8: Commit**

```bash
git add plugin_sdk/tool_contract.py opencomputer/agent/loop.py opencomputer/tools/ tests/test_tool_strict_mode.py
git commit -m "$(cat <<'EOF'
feat(tools): strict:true tool schemas (Item 3)

Add strict field to ToolSchema dataclass (default False, frozen-safe)
and BaseTool.strict_mode ClassVar (default True). The agent loop
copies each tool's strict_mode onto the ToolSchema before the provider
serializes the request. Anthropic then enforces input validation on
the tool, eliminating malformed-arg retries.

Per-tool audit: schemas that can't satisfy strict requirements
(free-form params, polymorphic args) opt out with strict_mode = False
+ explanatory comment. ≥80% strict adoption verified by test.

Spec: docs/superpowers/specs/2026-05-02-tool-use-contract-tightening-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Parallel-tool system prompt nudge

**Files:**
- Modify: `opencomputer/agent/prompts/base.j2`
- Test: `tests/test_system_prompt_parallel_nudge.py` (NEW)

**Approach.** Insert the canonical block from the doc as a new top-level section between `# Working rules` and `# Tone and style` in `base.j2`. Test uses `PromptBuilder().build()` — the project's existing API — to render the prompt and assert the block is present.

- [ ] **Step 4.1: Read the current `base.j2` to confirm section ordering**

```bash
grep -n "^# " opencomputer/agent/prompts/base.j2
```

Expected sections (verified): `# System info`, `# Identity and stance`, `# Working rules`, `# Tone and style`, etc.

- [ ] **Step 4.2: Insert the section**

Open `opencomputer/agent/prompts/base.j2`. Find the line `# Tone and style`. Insert the following block IMMEDIATELY BEFORE that line (preserve the blank line after the new section):

```jinja2
# Tool-call efficiency

<use_parallel_tool_calls>
For maximum efficiency, whenever you perform multiple independent operations,
invoke all relevant tools simultaneously rather than sequentially. Prioritize
calling tools in parallel whenever possible. For example, when reading 3
files, run 3 tool calls in parallel to read all 3 files into context at the
same time. When running multiple read-only commands like Glob or Grep,
always run all of the commands in parallel. Err on the side of maximizing
parallel tool calls rather than running too many tools sequentially.
</use_parallel_tool_calls>

```

(The example was adapted from the doc's `ls`/`list_dir` to OpenComputer's `Glob`/`Grep`.)

- [ ] **Step 4.3: Write the test**

Create `tests/test_system_prompt_parallel_nudge.py`:

```python
"""The base.j2 system prompt must include the <use_parallel_tool_calls> nudge."""
from opencomputer.agent.prompt_builder import PromptBuilder


def test_parallel_nudge_present_in_default_persona():
    builder = PromptBuilder()
    rendered = builder.build(active_persona_id="")  # default (not companion)
    assert "<use_parallel_tool_calls>" in rendered
    assert "invoke all relevant tools simultaneously" in rendered
    assert "</use_parallel_tool_calls>" in rendered


def test_parallel_nudge_present_in_companion_persona():
    builder = PromptBuilder()
    rendered = builder.build(active_persona_id="companion")
    # Section is unconditional — should be present in both modes
    assert "<use_parallel_tool_calls>" in rendered
```

- [ ] **Step 4.4: Run the test**

```bash
.venv/bin/pytest tests/test_system_prompt_parallel_nudge.py -v
```

Expected: 2 PASS.

- [ ] **Step 4.5: Run any prompt-rendering snapshot tests**

```bash
.venv/bin/pytest tests/ -k "prompt or render" -v 2>&1 | tail -20
```

Update any failing snapshot tests to match the new template (the new section is intentional and visible in the output).

- [ ] **Step 4.6: Commit**

```bash
git add opencomputer/agent/prompts/base.j2 tests/test_system_prompt_parallel_nudge.py
git commit -m "$(cat <<'EOF'
feat(prompt): parallel-tool-calls nudge in base system prompt (Item 4)

Insert <use_parallel_tool_calls> block (canonical wording from
Anthropic tool-use docs) as a new top-level section before
# Tone and style. Adapted the example from ls/list_dir to
OpenComputer's Glob/Grep names.

Bumps fan-out latency on multi-file reads and multi-grep queries.
Block is unconditional (present in both default and companion modes).

Spec: docs/superpowers/specs/2026-05-02-tool-use-contract-tightening-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Memory tool routing description audit

**Files:**
- Modify: `opencomputer/tools/memory_tool.py`
- Modify: `opencomputer/tools/recall.py`
- Modify: `opencomputer/tools/sessions.py`

**Approach.** Rewrite each tool's `description` field so the model knows which to call when. Each description gets a one-sentence purpose, then explicit "use when" / "do not use when" guidance referencing the alternatives by name. No code logic change — only string content.

- [ ] **Step 5.1: Rewrite `Memory` tool description**

In `opencomputer/tools/memory_tool.py`, find the existing `description=(...)` block for the `Memory` tool. Replace with:

```python
            description=(
                "Read or mutate the user's declarative memory file (MEMORY.md or USER.md).\n"
                "\n"
                "Use this when:\n"
                "  - The user says 'remember that X' or 'add to my notes that Y'\n"
                "  - You need to record a stable fact (preference, decision, contact info)\n"
                "  - You need to retrieve a specific stable fact from MEMORY.md / USER.md\n"
                "\n"
                "Do NOT use this for:\n"
                "  - Searching past conversations — use SessionsList + SessionsHistory\n"
                "  - Semantic recall over notes/journal — use Recall\n"
                "  - Storing per-conversation state — use the session naturally\n"
                "\n"
                "Actions:\n"
                "  add    — append an entry to the target file\n"
                "  replace — find+replace a substring in the target\n"
                "  remove — delete a block from the target\n"
                "  read   — return current contents of the target\n"
                "\n"
                "Files are bounded (MEMORY.md: ~4000 chars, USER.md: ~2000). "
                "Over-limit writes return an error; use remove to free space."
            ),
```

- [ ] **Step 5.2: Rewrite `Recall` tool description**

In `opencomputer/tools/recall.py`, replace the description block:

```python
            description=(
                "Vector search and append to the agent's long-term notes (MEMORY.md).\n"
                "\n"
                "Use this when:\n"
                "  - The user asks 'what did I say about X' (semantic query)\n"
                "  - You want to find related past turns by topic, not exact text\n"
                "  - You're recording a fact worth carrying across all future sessions\n"
                "\n"
                "Do NOT use this for:\n"
                "  - Reading the current session's own messages — that's already in context\n"
                "  - Listing recent sessions by time — use SessionsList\n"
                "  - Reading a known session's transcript — use SessionsHistory with the id\n"
                "  - Direct CRUD on MEMORY.md — use Memory (faster, no embedding cost)\n"
                "\n"
                "Three actions:\n"
                "- search: find past turns/messages by semantic similarity. Returns a "
                "mix of episodic summaries and raw message hits across all prior sessions.\n"
                "- note: append a fact / decision / preference to MEMORY.md so it "
                "carries across all future sessions. Use sparingly for things truly "
                "worth remembering — not for every interaction.\n"
                "- recall_session: fetch the history of a specific session by its id "
                "(returned by search). Use when you need the full context of a past "
                "conversation, not just the snippet."
            ),
```

- [ ] **Step 5.3: Rewrite the three `Sessions*` tool descriptions**

In `opencomputer/tools/sessions.py`, find each block and update.

`SessionsList`:

```python
            description=(
                "List recent conversation sessions for the current profile, ordered "
                "by last-activity timestamp (newest first).\n"
                "\n"
                "Use this when:\n"
                "  - The user references 'last time we talked' / 'the conversation about X'\n"
                "  - You need to find a session id before calling SessionsHistory\n"
                "\n"
                "Do NOT use this for:\n"
                "  - Semantic search across past notes — use Recall\n"
                "  - Reading stable facts from MEMORY.md — use Memory\n"
                "\n"
                "Returns session rows with id, title, platform, model, message_count, "
                "input_tokens, output_tokens, vibe, created_at, last_active_at."
            ),
```

`SessionsHistory`:

```python
            description=(
                "Read the most recent messages from a specific session by id.\n"
                "\n"
                "Use this when:\n"
                "  - You have a session_id (from SessionsList or Recall.search) and need\n"
                "    the actual conversation transcript\n"
                "  - The user wants to quote or recall context from a specific past chat\n"
                "\n"
                "Do NOT use this for:\n"
                "  - Searching across all sessions by topic — use Recall.search\n"
                "  - Listing recent sessions — use SessionsList\n"
                "\n"
                "Returns up to ``limit`` messages (default 30) with role, content, "
                "and tool_call/tool_result fields."
            ),
```

`SessionsStatus`:

```python
            description=(
                "Get metadata for a specific session by id (no message bodies).\n"
                "\n"
                "Use this when:\n"
                "  - You need session-level stats (token usage, message count, vibe)\n"
                "  - Verifying a session exists before calling SessionsHistory\n"
                "\n"
                "Do NOT use this for:\n"
                "  - Reading the actual messages — use SessionsHistory\n"
                "  - Listing many sessions — use SessionsList\n"
                "\n"
                "Returns: title, platform, model, message_count, input_tokens, "
                "output_tokens, vibe, created_at, last_active_at. Returns is_error=True "
                "with a clear message when the session id is not found in this profile's database."
            ),
```

- [ ] **Step 5.4: Run any tests that snapshot tool descriptions**

```bash
.venv/bin/pytest tests/ -k "tool or schema or description" -v 2>&1 | tail -20
```

Most should PASS. If any test asserts the EXACT description text (snapshot), update its expected value.

- [ ] **Step 5.5: Run full pytest suite**

```bash
.venv/bin/pytest tests/ 2>&1 | tail -20
```

Expected: all PASS.

- [ ] **Step 5.6: Commit**

```bash
git add opencomputer/tools/memory_tool.py opencomputer/tools/recall.py opencomputer/tools/sessions.py
git commit -m "$(cat <<'EOF'
docs(tools): memory tool routing descriptions (Item 5)

Rewrite Memory, Recall, SessionsList/History/Status descriptions
with explicit "use when" / "do not use when" routing per the spec's
matrix. Reduces wrong-tool calls when the model has multiple
overlapping memory subsystems available.

No behavior change — descriptions are model-facing only.

Spec: docs/superpowers/specs/2026-05-02-tool-use-contract-tightening-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Final verification

**Files:** none (verification only).

- [ ] **Step 6.1: Run full pytest suite**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
.venv/bin/pytest tests/ 2>&1 | tail -30
```

Expected: 5800+ tests PASS, 0 fail. Per the "no push without deep testing" memory rule, do NOT proceed if any fail.

- [ ] **Step 6.2: Run ruff lint**

```bash
.venv/bin/ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: no errors.

- [ ] **Step 6.3: Run ruff format check**

```bash
.venv/bin/ruff format --check opencomputer/ plugin_sdk/ extensions/ tests/
```

If any files need formatting, run `.venv/bin/ruff format <files>` and re-run the suite.

- [ ] **Step 6.4: Three-line subagent-honesty verification (per project memory rule)**

```bash
# Confirm code, not just commit messages, exists for every change
git log --oneline origin/main..HEAD
git diff origin/main..HEAD --stat
ls -la opencomputer/agent/prompt_caching.py tests/test_pause_refusal_stop_reasons.py tests/test_tool_strict_mode.py tests/test_system_prompt_parallel_nudge.py
```

Expected:
- 6 commits (spec + 5 features) in the log
- diff stat shows changes to: prompt_caching.py, loop.py, core.py, tool_contract.py, base.j2, memory_tool.py, recall.py, sessions.py, plus 4 test files
- All 4 new test files exist on disk

- [ ] **Step 6.5: Confirm parallel-nudge actually rendered in base.j2**

```bash
grep -c "<use_parallel_tool_calls>" opencomputer/agent/prompts/base.j2
```

Expected: `1` (opening tag — the closing tag also exists; either grep works).

- [ ] **Step 6.6: Optional manual smoke test for Item 1's cost win**

If you have an Anthropic API key set, run a multi-turn conversation against the live API and capture `Usage`. Look for `cache_read_input_tokens > 0` from turn 2 onward. This validates the tool-array cache is hitting in production. Skip if no API key — unit tests cover correctness.

---

## Self-review checklist

After completing all 6 tasks, verify:

- [ ] **Spec coverage:** Every numbered item (1-5) maps to a Task. Items 1-5 → Tasks 1-5. Verification → Task 6.
- [ ] **No placeholders:** No "TBD", "TODO", "implement later" anywhere. Real fixture pattern used in Task 2 (adapted from `test_loop_emits_bus_events.py`); real `PromptBuilder().build()` API used in Task 4.
- [ ] **Type consistency:** `StopReason.PAUSE_TURN` and `StopReason.REFUSAL` consistent throughout. `apply_full_cache_control` spelled the same in tests and provider call. `BaseTool.strict_mode` and `ToolSchema.strict` field linked via `_attach_strict` in loop.
- [ ] **Frozen-dataclass safety:** `ToolSchema` is `@dataclass(frozen=True, slots=True)`; the new `strict: bool = False` field is added directly to the dataclass (not a runtime attribute), so frozen+slots is satisfied.
- [ ] **TDD discipline:** Each task has Write-test → Run-fail → Implement → Run-pass → Commit cadence.
- [ ] **Backwards compatibility:** `apply_anthropic_cache_control()` unchanged. `to_anthropic_format()` defaults to no `strict` field (only emits when `ToolSchema.strict=True`). All existing tool constructors continue to work because `strict` has a default of `False`.
- [ ] **All risks from spec addressed:**
    - Strict failures → opt-out flag with comment + 80% threshold test
    - Cache reallocation reversible → `apply_anthropic_cache_control` preserved as fallback
    - pause_turn loop → cap at 3 with logged warning
    - Parallel nudge token cost → ~80 tokens, paid once per request, dwarfed by Item 1's savings

---

## Execution handoff

After saving and committing this plan:

**Plan complete and saved to `docs/superpowers/plans/2026-05-02-tool-use-contract-tightening-plan.md`. Two execution options:**

1. **Subagent-Driven (recommended for Task 3)** — Dispatch a fresh subagent per task; verify with the 3-line honesty check between tasks. Particularly suited to Task 3 (strict-mode audit), which touches ~40 tool files and benefits from fresh context per file.

2. **Inline Execution (recommended for Tasks 1, 2, 4, 5)** — Execute in this session using executing-plans, batched with checkpoints. These tasks are localized to a few files each.

**Recommended hybrid:** Inline for Tasks 1, 2, 4, 5; subagent for Task 3.
