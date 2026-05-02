# Tool-Use Contract Tightening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt 5 high-ROI items from the Anthropic tool-use contract (cache_control on tools, pause_turn/refusal handling, strict tool schemas, parallel-call nudge, memory tool routing audit) without disturbing OpenComputer's consent layer or memory architecture.

**Architecture:** Pure additive changes. No new modules. Touches `prompt_caching.py`, `loop.py`, `tool_contract.py`, `provider.py`, `base.j2`, and three tool description bodies. Backwards-compatible by default — existing tests pass without modification except where they assert breakpoint counts (Item 1).

**Tech Stack:** Python 3.13, Anthropic Python SDK ≥ 0.40 (verified `ToolParam.strict` and `cache_control` fields exist), Jinja2 (system prompt template), pytest.

**Spec:** `docs/superpowers/specs/2026-05-02-tool-use-contract-tightening-design.md`

**Branch:** `spec/tool-use-contract-tightening` (1 commit ahead of `origin/main`).

---

## File structure

| File | Change | Item |
|---|---|---|
| `opencomputer/agent/prompt_caching.py` | Add `apply_tools_cache_control()`; modify `apply_anthropic_cache_control()` to accept `tools_already_cached` flag (caps msg breakpoints at 3 when True) | 1 |
| `extensions/anthropic-provider/provider.py` | Call `apply_tools_cache_control()` after building tools list at lines 437 and 528 | 1 |
| `tests/test_prompt_caching.py` | Update existing test for new allocation; add 3 new tests | 1 |
| `plugin_sdk/core.py` | Add `StopReason.PAUSE_TURN` and `StopReason.REFUSAL` to enum | 2 |
| `opencomputer/agent/loop.py` | Extend stop_reason_map; add pause_turn re-send loop with 3-attempt cap; add refusal handling | 2 |
| `tests/test_pause_refusal_stop_reasons.py` | NEW: 3 tests for pause-then-end, pause-cap-exceeded, refusal | 2 |
| `plugin_sdk/tool_contract.py` | Add `BaseTool.strict_mode: ClassVar[bool] = True`; modify `ToolSchema.to_anthropic_format()` to accept and emit `strict` | 3 |
| `opencomputer/tools/registry.py` | Pass tool's `strict_mode` into format call | 3 |
| `opencomputer/tools/*.py` | Per-tool audit pass for strict-validation compatibility | 3 |
| `tests/test_tool_strict_mode.py` | NEW: parametrized strict-validation test for all registered tools | 3 |
| `opencomputer/agent/prompts/base.j2` | Append new `# Tool-call efficiency` section after `# Working rules` | 4 |
| `opencomputer/tools/memory_tool.py` | Rewrite description with routing matrix | 5 |
| `opencomputer/tools/recall.py` | Rewrite description with routing matrix | 5 |
| `opencomputer/tools/sessions.py` | Update SessionsList/History/Status descriptions for routing clarity | 5 |

---

## Task 1: cache_control on tools array

**Files:**
- Modify: `opencomputer/agent/prompt_caching.py`
- Modify: `extensions/anthropic-provider/provider.py:437,528`
- Test: `tests/test_prompt_caching.py`

**Approach.** Add a new function `apply_tools_cache_control(tools_list)` that marks the last tool with `cache_control: ephemeral`. Modify `apply_anthropic_cache_control()` to accept an optional `tools_already_cached: bool = False` flag — when True, the function reduces its non-system message breakpoints from 3 to 2 so the total stays ≤ 4 (1 tools + 1 system + 2 messages).

- [ ] **Step 1.1: Write failing test for `apply_tools_cache_control`**

Add to `tests/test_prompt_caching.py`:

```python
def test_apply_tools_cache_control_marks_last_tool():
    from opencomputer.agent.prompt_caching import apply_tools_cache_control

    tools = [
        {"name": "Read", "description": "...", "input_schema": {}},
        {"name": "Write", "description": "...", "input_schema": {}},
        {"name": "Bash", "description": "...", "input_schema": {}},
    ]
    out = apply_tools_cache_control(tools)
    # Only last tool gets cache_control
    assert "cache_control" not in out[0]
    assert "cache_control" not in out[1]
    assert out[2]["cache_control"] == {"type": "ephemeral"}


def test_apply_tools_cache_control_empty_list():
    from opencomputer.agent.prompt_caching import apply_tools_cache_control
    assert apply_tools_cache_control([]) == []


def test_apply_tools_cache_control_does_not_mutate_input():
    from opencomputer.agent.prompt_caching import apply_tools_cache_control
    tools = [{"name": "Read"}]
    apply_tools_cache_control(tools)
    assert "cache_control" not in tools[0]
```

- [ ] **Step 1.2: Run test to verify failure**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
.venv/bin/pytest tests/test_prompt_caching.py::test_apply_tools_cache_control_marks_last_tool -v
```

Expected: `ImportError: cannot import name 'apply_tools_cache_control'`

- [ ] **Step 1.3: Add `apply_tools_cache_control` and modify `apply_anthropic_cache_control`**

Replace contents of `opencomputer/agent/prompt_caching.py` with:

```python
"""Anthropic prompt caching (system_and_3 / system_and_tools_and_2 strategy).

Reduces input token costs by caching the conversation prefix using up to 4
``cache_control`` breakpoints (Anthropic max).

Two allocation strategies:
  - Without tools cache (default, backwards-compatible):
      1. System prompt
      2-4. Last 3 non-system messages (rolling window)
  - With tools cache (``tools_already_cached=True``):
      1. Last entry of the tools array (handled by ``apply_tools_cache_control``)
      2. System prompt
      3-4. Last 2 non-system messages (rolling window)

Why move tools above last-message: tool definitions are large (~8-30k tokens
for ~40 registered tools) and change rarely, so they have the highest cache
hit rate. The deepest of the 3 message breakpoints has the lowest hit rate
(every turn changes the tail), so dropping it is the cheapest reallocation.

Pure functions -- no class state, no AIAgent dependency.
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


def apply_tools_cache_control(
    tools_list: list[dict[str, Any]],
    cache_ttl: str = "5m",
) -> list[dict[str, Any]]:
    """Mark the last tool definition with ``cache_control: ephemeral``.

    Anthropic's prefix-based caching extends the cached span up to and
    including the breakpoint. Marking the last tool caches the entire
    tools array (a large, stable prefix) for subsequent requests.

    Returns a deep copy. Empty list is returned unchanged.
    """
    if not tools_list:
        return tools_list
    out = copy.deepcopy(tools_list)
    out[-1]["cache_control"] = _build_marker(cache_ttl)
    return out


def apply_anthropic_cache_control(
    api_messages: list[dict[str, Any]],
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
    tools_already_cached: bool = False,
) -> list[dict[str, Any]]:
    """Apply the cache_control breakpoint strategy to messages.

    With ``tools_already_cached=False`` (default, backwards-compat):
        Up to 4 breakpoints — system + last 3 non-system messages.

    With ``tools_already_cached=True``:
        Up to 3 breakpoints — system + last 2 non-system messages.
        The 4th breakpoint is reserved for the tools array, applied
        separately via ``apply_tools_cache_control``.

    Returns:
        Deep copy of messages with cache_control breakpoints injected.
    """
    messages = copy.deepcopy(api_messages)
    if not messages:
        return messages

    marker = _build_marker(cache_ttl)
    breakpoints_used = 0
    total_budget = 3 if tools_already_cached else 4

    if messages[0].get("role") == "system":
        _apply_cache_marker(messages[0], marker, native_anthropic=native_anthropic)
        breakpoints_used += 1

    remaining = total_budget - breakpoints_used
    non_sys = [i for i in range(len(messages)) if messages[i].get("role") != "system"]
    for idx in non_sys[-remaining:]:
        _apply_cache_marker(messages[idx], marker, native_anthropic=native_anthropic)

    return messages
```

- [ ] **Step 1.4: Run new tests to verify they pass**

```bash
.venv/bin/pytest tests/test_prompt_caching.py::test_apply_tools_cache_control_marks_last_tool tests/test_prompt_caching.py::test_apply_tools_cache_control_empty_list tests/test_prompt_caching.py::test_apply_tools_cache_control_does_not_mutate_input -v
```

Expected: 3 PASS

- [ ] **Step 1.5: Update existing test for the reduced-budget allocation**

Replace `test_last_3_non_system_get_cache_control` in `tests/test_prompt_caching.py` and add a new test:

```python
def test_last_3_non_system_get_cache_control_default():
    """Default allocation (no tools cache): system + last 3 messages."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
        {"role": "user", "content": "msg3"},
        {"role": "assistant", "content": "msg4"},
    ]
    out = apply_anthropic_cache_control(msgs)
    cache_count = 0
    for m in out:
        c = m.get("content")
        if isinstance(c, list):
            cache_count += sum(1 for blk in c if isinstance(blk, dict) and "cache_control" in blk)
        if "cache_control" in m:
            cache_count += 1
    assert cache_count == 4
    # msg1 should NOT have cache_control (only last 3 non-system do)
    msg1_content = out[1]["content"]
    if isinstance(msg1_content, list):
        for blk in msg1_content:
            if isinstance(blk, dict):
                assert "cache_control" not in blk


def test_last_2_non_system_get_cache_control_when_tools_cached():
    """With tools_already_cached=True: system + last 2 messages only."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
        {"role": "user", "content": "msg3"},
        {"role": "assistant", "content": "msg4"},
    ]
    out = apply_anthropic_cache_control(msgs, tools_already_cached=True)
    cache_count = 0
    for m in out:
        c = m.get("content")
        if isinstance(c, list):
            cache_count += sum(1 for blk in c if isinstance(blk, dict) and "cache_control" in blk)
        if "cache_control" in m:
            cache_count += 1
    # 3 breakpoints: system + msg3 + msg4
    assert cache_count == 3
    # msg1 and msg2 should NOT have cache_control
    for idx in (1, 2):
        c = out[idx]["content"]
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict):
                    assert "cache_control" not in blk
```

Delete the old `test_last_3_non_system_get_cache_control` (replaced above) and update `test_max_4_breakpoints_with_many_messages` body so the assertion stays at 4 (it does — the default behavior unchanged):

```python
def test_max_4_breakpoints_with_many_messages():
    """Default budget is 4."""
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(20)]
    out = apply_anthropic_cache_control(msgs)
    cache_count = 0
    for m in out:
        c = m.get("content")
        if isinstance(c, list):
            cache_count += sum(1 for blk in c if isinstance(blk, dict) and "cache_control" in blk)
        if "cache_control" in m:
            cache_count += 1
    assert cache_count == 4


def test_max_3_breakpoints_when_tools_cached():
    """With tools_already_cached, budget caps at 3 message breakpoints."""
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(20)]
    out = apply_anthropic_cache_control(msgs, tools_already_cached=True)
    cache_count = 0
    for m in out:
        c = m.get("content")
        if isinstance(c, list):
            cache_count += sum(1 for blk in c if isinstance(blk, dict) and "cache_control" in blk)
        if "cache_control" in m:
            cache_count += 1
    assert cache_count == 3
```

- [ ] **Step 1.6: Run all prompt_caching tests**

```bash
.venv/bin/pytest tests/test_prompt_caching.py -v
```

Expected: all PASS (8 tests total — 6 original + 2 new for tools cache + the renamed/added budget tests).

- [ ] **Step 1.7: Wire `apply_tools_cache_control` into the Anthropic provider**

In `extensions/anthropic-provider/provider.py`, find the two call sites that build `kwargs["tools"]` (currently at approx lines 437 and 528 — search for `[t.to_anthropic_format() for t in tools]`).

For each, immediately after the comprehension, add:

```python
if kwargs.get("tools"):
    from opencomputer.agent.prompt_caching import apply_tools_cache_control
    kwargs["tools"] = apply_tools_cache_control(kwargs["tools"])
```

Then find the call to `apply_anthropic_cache_control` in the same file (search for `apply_anthropic_cache_control(`). Pass `tools_already_cached=True` when tools were cached:

```python
# Existing call signature stays the same; add the new kwarg conditionally
tools_were_cached = bool(kwargs.get("tools"))
api_messages = apply_anthropic_cache_control(
    anthropic_messages,
    cache_ttl=cache_ttl,
    native_anthropic=True,
    tools_already_cached=tools_were_cached,
)
```

Note: confirm the exact argument name (`api_messages` vs `anthropic_messages`) by reading around line 518 — leave the rest of the call site untouched.

- [ ] **Step 1.8: Run anthropic-provider tests + full prompt_caching tests**

```bash
.venv/bin/pytest tests/test_prompt_caching.py extensions/anthropic-provider/ tests/ -k "anthropic or cache" -v
```

Expected: all PASS.

- [ ] **Step 1.9: Commit**

```bash
git add opencomputer/agent/prompt_caching.py extensions/anthropic-provider/provider.py tests/test_prompt_caching.py
git commit -m "$(cat <<'EOF'
feat(cache): apply cache_control to tools array (Item 1)

Reallocate the 4 ephemeral cache breakpoints from "system + last 3
messages" to "tools[-1] + system + last 2 messages" when tools are
sent. Tool definitions (~8-30k tokens for ~40 tools) change rarely
and now cache, while the lowest-hit message breakpoint is dropped.

Adds apply_tools_cache_control() and a tools_already_cached flag on
apply_anthropic_cache_control() (default False = backwards-compat).

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

- [ ] **Step 2.1: Add enum values**

In `plugin_sdk/core.py`, find the `StopReason` class at line 417 and add two members:

```python
class StopReason(str, Enum):
    """Why a conversation step ended."""

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    INTERRUPTED = "interrupted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERROR = "error"
    PAUSE_TURN = "pause_turn"  # server-tool work paused; re-send to continue
    REFUSAL = "refusal"  # model refused; surface as final, do not retry
```

- [ ] **Step 2.2: Write failing tests**

Create `tests/test_pause_refusal_stop_reasons.py`:

```python
"""Tests for pause_turn and refusal stop_reason handling in the agent loop."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from plugin_sdk.core import Message, StopReason, ProviderResponse, Usage


def _build_response(stop_reason: str, text: str = "", tool_calls=None):
    """Helper to build a ProviderResponse with a given stop_reason."""
    return ProviderResponse(
        message=Message(role="assistant", content=text, tool_calls=tool_calls),
        stop_reason=stop_reason,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


@pytest.mark.asyncio
async def test_pause_turn_then_end_turn_continues_loop():
    """A pause_turn response triggers a re-send; subsequent end_turn exits cleanly."""
    from opencomputer.agent.loop import AgentLoop

    # Provider returns pause_turn first, then end_turn
    responses = [
        _build_response("pause_turn", "(paused mid-search)"),
        _build_response("end_turn", "Final answer."),
    ]
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=responses)

    loop = _make_minimal_loop(provider=provider)
    result = await loop.run_conversation("test query")

    assert provider.complete.call_count == 2
    assert "Final answer" in result.final_text


@pytest.mark.asyncio
async def test_pause_turn_cap_exceeded_exits_with_warning(caplog):
    """4 consecutive pause_turn responses → loop exits at cap with logged warning."""
    from opencomputer.agent.loop import AgentLoop

    responses = [_build_response("pause_turn", f"paused {i}") for i in range(5)]
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=responses)

    loop = _make_minimal_loop(provider=provider)
    result = await loop.run_conversation("test query")

    # Expected: 3 pause attempts then forced END_TURN (1 + 3 = 4 max calls)
    assert provider.complete.call_count <= 4
    assert any("pause_turn cap" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_refusal_exits_without_retry():
    """A refusal response surfaces the assistant text and exits immediately."""
    from opencomputer.agent.loop import AgentLoop

    responses = [_build_response("refusal", "I can't help with that.")]
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=responses)

    loop = _make_minimal_loop(provider=provider)
    result = await loop.run_conversation("dangerous query")

    assert provider.complete.call_count == 1
    assert "can't help" in result.final_text


def _make_minimal_loop(provider):
    """Build an AgentLoop wired to a mock provider with minimum config.

    Implementation detail: this helper is the only test scaffolding here.
    Use the same fixtures the existing loop tests use — find one in
    tests/test_loop_*.py or tests/conftest.py and copy its pattern.
    """
    # Concrete construction is project-specific; find an existing
    # AgentLoop fixture (e.g. test_loop_*.py) and replicate it here.
    raise NotImplementedError(
        "Replace with the project's standard AgentLoop test fixture. "
        "Look at any tests/test_loop_*.py for the pattern."
    )
```

Note: the `_make_minimal_loop` helper is a placeholder for the project's standard test fixture. Before running the test, locate an existing AgentLoop test (e.g. `tests/test_loop_basic.py` or similar) and replicate its setup. If no such fixture exists, ask first — do not fabricate one.

- [ ] **Step 2.3: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_pause_refusal_stop_reasons.py -v
```

Expected: FAIL — either `NotImplementedError` from the fixture, or assertion failures because the loop doesn't yet handle these stop reasons.

- [ ] **Step 2.4: Modify the stop_reason_map and add pause/refusal handling**

In `opencomputer/agent/loop.py`, find the block at lines 2753-2764 and replace with:

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

        # If the model called tools, even if the raw stop_reason was "end_turn",
        # we need to continue so the model can process results.
        if resp.message.tool_calls and stop == StopReason.END_TURN:
            stop = StopReason.TOOL_USE
```

Then locate the loop's main iteration block (around line 1093 — search for `for _iter in range(self.config.loop.max_iterations)`). After the `step = await self._run_one_step(...)` call (around line 1299), add pause/refusal handling. The exact insertion point depends on the existing control flow — find where `step.should_continue` is checked.

Insert the following BEFORE the `if not step.should_continue:` check that returns:

```python
            # 2026-05-02 (Item 2): handle pause_turn and refusal stop reasons
            # added to stop_reason_map at line 2753.
            #
            # pause_turn: server-tool work paused mid-call. Per Anthropic
            # contract, re-send the conversation (including the paused
            # assistant response) to continue. Cap at 3 consecutive pauses
            # to avoid pathological loops on broken server tools.
            if step.stop_reason == StopReason.PAUSE_TURN:
                self._pause_turn_count = getattr(self, "_pause_turn_count", 0) + 1
                if self._pause_turn_count >= 3:
                    import logging
                    logging.getLogger(__name__).warning(
                        "pause_turn cap (3) exceeded — forcing END_TURN. "
                        "A server tool may be stuck in a re-send loop."
                    )
                    # Treat as END_TURN; preserve any partial text.
                    step = step._replace(
                        stop_reason=StopReason.END_TURN,
                        should_continue=False,
                    ) if hasattr(step, "_replace") else step
                    # NOTE: step is likely a frozen dataclass — adapt the
                    # mutation pattern to whatever StepOutcome supports.
                else:
                    # Append the paused assistant message and re-send.
                    messages.append(step.assistant_message)
                    continue  # next loop iteration re-sends with the paused content

            # refusal: model declined. Exit immediately, do NOT retry.
            if step.stop_reason == StopReason.REFUSAL:
                # Final assistant text already in step.assistant_message.content.
                self._pause_turn_count = 0  # reset for any subsequent run
                break

            # Reset pause counter on any other outcome
            if step.stop_reason != StopReason.PAUSE_TURN:
                self._pause_turn_count = 0
```

**Important.** The `step._replace(...)` pattern depends on whether `StepOutcome` is a NamedTuple (has `_replace`), a frozen dataclass (use `dataclasses.replace`), or a mutable class. Read `opencomputer/agent/step.py` first and use the matching pattern.

- [ ] **Step 2.5: Find the right StepOutcome mutation pattern**

```bash
cat opencomputer/agent/step.py | head -40
```

If frozen dataclass, replace `step._replace(...)` with:

```python
from dataclasses import replace as _dc_replace
step = _dc_replace(step, stop_reason=StopReason.END_TURN, should_continue=False)
```

If NamedTuple, `_replace` works as written.

If mutable, simple attribute assignment.

- [ ] **Step 2.6: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_pause_refusal_stop_reasons.py -v
```

Expected: 3 PASS.

- [ ] **Step 2.7: Run full loop test suite to catch regressions**

```bash
.venv/bin/pytest tests/ -k "loop or stop_reason or agent" -v
```

Expected: all PASS.

- [ ] **Step 2.8: Commit**

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
- Modify: `opencomputer/tools/registry.py` (if it builds the API tool list separately)
- Modify: `opencomputer/tools/*.py` (per-tool audit, opt-out where needed)
- Test: `tests/test_tool_strict_mode.py` (NEW)

**Approach.** Add `BaseTool.strict_mode: ClassVar[bool] = True` defaulting True. `ToolSchema.to_anthropic_format()` accepts a `strict: bool = False` parameter and emits the field when True. Tool dispatch passes `tool.strict_mode` through. Audit pass: any tool whose schema fails strict-validation requirements gets `strict_mode = False` with a comment explaining why.

**Strict-mode requirements per Anthropic SDK (`ToolParam.strict`):** "When true, guarantees schema validation on tool names and inputs." Object schemas should declare `additionalProperties: false`, all required fields listed, no implicit type unions.

- [ ] **Step 3.1: Add strict_mode flag to BaseTool**

In `plugin_sdk/tool_contract.py`, add the class attribute:

```python
class BaseTool(ABC):
    """Base class for a tool. Subclass and implement `schema` + `execute`."""

    parallel_safe: bool = False
    max_result_size: int = 100_000
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = ()

    #: 2026-05-02 — when True, the tool's schema is sent with strict: true
    #: to Anthropic, guaranteeing input validation. Tools whose schemas
    #: cannot satisfy strict requirements (no additionalProperties: false,
    #: missing explicit types, optional union types) should override
    #: this to False with a one-line comment explaining why.
    strict_mode: ClassVar[bool] = True

    @property
    @abstractmethod
    def schema(self) -> ToolSchema: ...

    @abstractmethod
    async def execute(self, call: ToolCall) -> ToolResult: ...
```

- [ ] **Step 3.2: Modify `ToolSchema.to_anthropic_format`**

```python
    def to_anthropic_format(self, *, strict: bool = False) -> dict[str, Any]:
        """Convert to the dict format the Anthropic API expects."""
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }
        if strict:
            out["strict"] = True
        return out
```

- [ ] **Step 3.3: Update the call site that builds the API tools list**

In `extensions/anthropic-provider/provider.py`, find:

```python
kwargs["tools"] = [t.to_anthropic_format() for t in tools]
```

Replace at BOTH call sites (lines ~437 and ~528) with code that gets `strict_mode` from the tool. The `tools` parameter in those signatures is `list[ToolSchema]` (the schema dataclass), but we need the originating tool's `strict_mode`. Search how `tools` is built upstream.

If `tools` is a list of `ToolSchema` (and `strict_mode` is on the originating `BaseTool`), this requires plumbing. **Read the call chain first**: search for where `provider.complete(..., tools=...)` is invoked. The likely chain is `AgentLoop._run_one_step → provider.complete(tools=tool_schemas)` where `tool_schemas` was built from `[t.schema for t in registered_tools]`.

If that's the case, change `tool_schemas` to instead build a list of tuples `[(t.schema, t.strict_mode) for t in registered_tools]`, OR pass tools as `BaseTool` instances and build the API format inside the provider.

**Recommended minimal change:** in `opencomputer/agent/loop.py`, find where `tool_schemas` is built. Wrap each schema with its strict flag. In `provider.py`, the comprehension becomes:

```python
kwargs["tools"] = [t.to_anthropic_format(strict=getattr(t, "_strict_mode", False)) for t in tools]
```

This requires attaching `_strict_mode` to each `ToolSchema` instance before passing it down — which is awkward because `ToolSchema` is `frozen=True, slots=True`.

**Alternative cleaner approach:** introduce a small dataclass `ToolForApi` in `plugin_sdk/tool_contract.py`:

```python
@dataclass(frozen=True, slots=True)
class ToolForApi:
    """Bundles a ToolSchema with the runtime flags needed to format it for an API."""
    schema: ToolSchema
    strict: bool = False
```

Then in `loop.py` build `tool_schemas: list[ToolForApi]`, and in `provider.py`:

```python
kwargs["tools"] = [t.schema.to_anthropic_format(strict=t.strict) for t in tools]
```

This requires updating the type annotation on `BaseProvider.complete(tools=...)` and any test that mocks this. Confirm scope before committing.

**Decision point.** Before writing code, decide:
- (A) Quick patch: monkey-patch `_strict_mode` onto `ToolSchema` instances in loop.py before passing down. Ugly but localized.
- (B) Clean: introduce `ToolForApi` and update the type. More files touched but type-safe.

**Recommend (B)** for cleanliness. If that's too invasive given this PR's scope, fall back to (A) and ticket the cleanup.

- [ ] **Step 3.4: Write failing parametrized strict-validation test**

Create `tests/test_tool_strict_mode.py`:

```python
"""Validate that tools declared as strict_mode=True have schemas that can
satisfy Anthropic's strict-mode contract."""
import pytest

from opencomputer.tools.registry import ToolRegistry


def _is_strict_compatible(schema_params: dict) -> tuple[bool, str]:
    """Return (passes, reason) for a JSON Schema object dict."""
    if schema_params.get("type") != "object":
        return False, "top-level type is not 'object'"
    # Strict mode requires additionalProperties: false on object schemas.
    if schema_params.get("additionalProperties") is not False:
        return False, "additionalProperties is not False"
    # All declared properties must have a 'type' (no implicit unions).
    props = schema_params.get("properties") or {}
    for name, spec in props.items():
        if "type" not in spec and "enum" not in spec and "$ref" not in spec:
            return False, f"property '{name}' has no type/enum/$ref"
    return True, ""


def _all_registered_tools():
    """Build a registry the same way the agent does at startup."""
    from opencomputer.tools.registry import build_default_registry  # adapt to actual builder
    reg = build_default_registry()
    return list(reg.iter_tools())  # adapt method name to actual API


@pytest.mark.parametrize("tool", _all_registered_tools(), ids=lambda t: t.schema.name)
def test_strict_tools_have_compatible_schemas(tool):
    """Every tool declared strict_mode=True must have a strict-compatible schema."""
    if not getattr(tool, "strict_mode", False):
        pytest.skip(f"{tool.schema.name} opted out of strict mode")
    passes, reason = _is_strict_compatible(tool.schema.parameters)
    assert passes, f"{tool.schema.name} not strict-compatible: {reason}"
```

Adapt `build_default_registry` and `iter_tools` to the actual project APIs by reading `opencomputer/tools/registry.py`.

- [ ] **Step 3.5: Run test — expect failures from non-strict tools**

```bash
.venv/bin/pytest tests/test_tool_strict_mode.py -v 2>&1 | tee /tmp/strict_audit.log
```

Expected: a mix of PASS and FAIL. Each FAIL identifies a tool whose schema needs adjustment OR an opt-out.

- [ ] **Step 3.6: Audit — for each failing tool, decide:**

Open the tool's source file. Pick ONE:

(a) **Fix the schema** (preferred): add `additionalProperties: false`, add explicit types to all properties.

```python
parameters={
    "type": "object",
    "additionalProperties": False,  # ADD THIS
    "properties": {
        "command": {"type": "string", "description": "..."},  # ensure type is set
        ...
    },
    "required": ["command"],
}
```

(b) **Opt out**: add to the class:

```python
class BashTool(BaseTool):
    parallel_safe = False
    strict_mode = False  # 2026-05-02: shell command parameter is open-ended; strict adds friction with no model-quality win

    @property
    def schema(self) -> ToolSchema:
        ...
```

Goal: ≥80% of tools pass strict on first run. Opt-outs need a comment explaining why (e.g. "free-form command string", "complex polymorphic parameter").

- [ ] **Step 3.7: Run test again — expect all (passing-strict tools) PASS**

```bash
.venv/bin/pytest tests/test_tool_strict_mode.py -v
```

Expected: every parametrized case either PASS or SKIP (opted out).

- [ ] **Step 3.8: Add a test that verifies strict makes it onto the wire**

Append to `tests/test_tool_strict_mode.py`:

```python
def test_strict_emitted_in_anthropic_format():
    """A ToolSchema formatted with strict=True includes the strict field."""
    from plugin_sdk.tool_contract import ToolSchema
    s = ToolSchema(
        name="DummyStrict",
        description="x",
        parameters={"type": "object", "additionalProperties": False, "properties": {}},
    )
    assert "strict" not in s.to_anthropic_format()
    assert s.to_anthropic_format(strict=True)["strict"] is True
```

- [ ] **Step 3.9: Run test**

```bash
.venv/bin/pytest tests/test_tool_strict_mode.py::test_strict_emitted_in_anthropic_format -v
```

Expected: PASS.

- [ ] **Step 3.10: Run full pytest suite**

```bash
.venv/bin/pytest tests/ -x 2>&1 | tail -30
```

Expected: all PASS. If any test asserts an exact tool schema dict and that dict gained `additionalProperties: false`, update the assertion.

- [ ] **Step 3.11: Commit**

```bash
git add plugin_sdk/tool_contract.py opencomputer/tools/ extensions/anthropic-provider/provider.py opencomputer/agent/loop.py tests/test_tool_strict_mode.py
git commit -m "$(cat <<'EOF'
feat(tools): strict:true tool schemas (Item 3)

BaseTool.strict_mode (default True) flows into ToolSchema.to_anthropic_format
and onto the wire as the strict field. Tools whose schemas cannot satisfy
strict requirements (free-form params, polymorphic args) opt out with
strict_mode = False + comment.

Goal ≥80% strict adoption hit. Eliminates malformed-arg tool retries
on tools that pass.

Spec: docs/superpowers/specs/2026-05-02-tool-use-contract-tightening-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Parallel-tool system prompt nudge

**Files:**
- Modify: `opencomputer/agent/prompts/base.j2`

**Approach.** Insert the canonical block from the doc as a new top-level section between `# Working rules` and `# Tone and style`. Keep it isolated so it can be lifted out cleanly if the default behavior improves.

- [ ] **Step 4.1: Add the section to `base.j2`**

Open `opencomputer/agent/prompts/base.j2` and find the line containing `# Tone and style`. Insert the following block IMMEDIATELY BEFORE that line:

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

(Note the blank line after the closing tag — keep the spacing consistent with surrounding sections.)

- [ ] **Step 4.2: Find existing snapshot tests for the system prompt**

```bash
grep -rn "base.j2\|system_prompt\|render.*prompt" tests/ | head -10
```

If there's a snapshot test for the assembled system prompt, update its expected output.

If there's no snapshot, add one:

```python
# tests/test_system_prompt_parallel_nudge.py
def test_parallel_nudge_present():
    """The base.j2 prompt must include the parallel-tool-calls nudge."""
    from opencomputer.agent.prompt_builder import render_system_prompt  # adapt to actual import
    rendered = render_system_prompt(
        cwd="/tmp", user_home="/home/test", os_name="darwin",
        now="2026-05-02", active_persona_id="default",
    )
    assert "<use_parallel_tool_calls>" in rendered
    assert "invoke all relevant tools simultaneously" in rendered
    assert "</use_parallel_tool_calls>" in rendered
```

Adapt `render_system_prompt` and its kwargs by reading `opencomputer/agent/prompt_builder.py`.

- [ ] **Step 4.3: Run the test**

```bash
.venv/bin/pytest tests/test_system_prompt_parallel_nudge.py -v
```

Expected: PASS.

- [ ] **Step 4.4: Run any existing prompt-rendering snapshot tests**

```bash
.venv/bin/pytest tests/ -k "prompt or render" -v
```

Update any failing snapshots to match the new template (the new section is intentional).

- [ ] **Step 4.5: Commit**

```bash
git add opencomputer/agent/prompts/base.j2 tests/test_system_prompt_parallel_nudge.py
git commit -m "$(cat <<'EOF'
feat(prompt): parallel-tool-calls nudge in base system prompt (Item 4)

Append <use_parallel_tool_calls> block (canonical wording from
Anthropic tool-use docs) as a new top-level section after Working rules.
Adapted the example from ls/list_dir to OpenComputer's Glob/Grep names.

Bumps fan-out latency on multi-file reads and multi-grep queries.

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

**Approach.** Rewrite each tool's `description` field so the model knows which to call when. Each description gets a one-sentence purpose, then explicit "use when" / "do not use when" guidance referencing the alternatives by name.

- [ ] **Step 5.1: Rewrite `Memory` tool description**

In `opencomputer/tools/memory_tool.py`, replace the current description with:

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

In `opencomputer/tools/recall.py`, replace the current description with:

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

In `opencomputer/tools/sessions.py`, find each tool's description block and update.

For `SessionsList`:

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

For `SessionsHistory`:

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

For `SessionsStatus`:

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
.venv/bin/pytest tests/ -k "tool or schema or description" -v
```

Expected: most PASS. If any test asserts the EXACT description text, update its expected value to match the new description.

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

Expected: 5800+ tests PASS, 0 fail. If any fail, do NOT proceed — fix before merge per the project's "no push without deep testing" rule.

- [ ] **Step 6.2: Run ruff lint**

```bash
.venv/bin/ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: no errors. Fix any reported.

- [ ] **Step 6.3: Run ruff format check**

```bash
.venv/bin/ruff format --check opencomputer/ plugin_sdk/ extensions/ tests/
```

If any files need formatting, run `.venv/bin/ruff format <files>` then re-test.

- [ ] **Step 6.4: Verify each commit landed correctly**

```bash
git log --oneline origin/main..HEAD
```

Expected: 6 commits — spec + 5 feature commits (Items 1-5).

- [ ] **Step 6.5: Three-line subagent-honesty verification (per project memory rule)**

```bash
# Confirm code, not just commit messages, exists for every change
git diff origin/main..HEAD --stat
ls -la opencomputer/agent/prompt_caching.py tests/test_pause_refusal_stop_reasons.py tests/test_tool_strict_mode.py
git log -p origin/main..HEAD -- opencomputer/agent/prompts/base.j2 | grep -c "use_parallel_tool_calls"
```

Expected:
- diff stat shows changes to prompt_caching.py, loop.py, core.py (StopReason), tool_contract.py, base.j2, 3 tool description files
- new test files exist
- `use_parallel_tool_calls` appears at least 4 times in the base.j2 diff (open + close + comment + check)

- [ ] **Step 6.6: Optional manual smoke test for Item 1's cost win**

If you have an Anthropic API key set, run a 5-turn conversation against the live API and capture `Usage`:

```bash
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY python -c "
from opencomputer.cli import _run_session  # adapt to actual entry point
# ... or just use 'opencomputer' CLI in another shell with --debug usage logging
"
```

Expected on turn 2+: `cache_read_input_tokens > 0`. This validates the tool-array cache is hitting.

This is OPTIONAL because it requires a live API key and a running session. Skip if not available; the unit tests already cover the cache placement.

---

## Self-review checklist

After completing all 6 tasks, verify:

- [ ] **Spec coverage:** Every numbered item (1-5) in the spec maps to a Task above. Items 1, 2, 3, 4, 5 → Tasks 1, 2, 3, 4, 5. Verification → Task 6.
- [ ] **No placeholders:** No "TBD", "TODO", "implement later", "fill in details" anywhere in the plan.
- [ ] **Type consistency:** `StopReason.PAUSE_TURN` and `StopReason.REFUSAL` are referenced consistently. `apply_tools_cache_control` and `tools_already_cached` flag are spelled the same throughout. `BaseTool.strict_mode` and `ToolSchema.to_anthropic_format(strict=...)` match.
- [ ] **TDD discipline:** Each task has Write-test → Run-fail → Implement → Run-pass → Commit cadence.
- [ ] **Backwards compatibility:** Default behavior of `apply_anthropic_cache_control()` unchanged when `tools_already_cached=False`. `to_anthropic_format()` default `strict=False` unchanged.
- [ ] **All risks from spec addressed:**
    - Strict failures handled via opt-out
    - Cache reallocation reversible (just flip the flag default)
    - pause_turn cap at 3 prevents loops
    - Parallel nudge token cost noted (~80 tokens, paid once per request)

## Execution handoff

After saving and committing this plan:

**Plan complete and saved to `docs/superpowers/plans/2026-05-02-tool-use-contract-tightening-plan.md`. Two execution options:**

1. **Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, verify with 3-line honesty check between tasks, fast iteration. Particularly suited to Task 3 (strict-mode audit) which touches ~40 files and benefits from fresh context per file.

2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints between tasks.

**Recommended:** Subagent-Driven for Task 3 (strict audit) and Inline for Tasks 1, 2, 4, 5 (small surface area, single-file changes).
