# OpenClaw Tier 1 Port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port 8 selective OpenClaw capabilities into OpenComputer that aren't covered by the Hermes Tier 1+2+3 megamerge or earlier OC work, after evidence-based gap analysis.

**Architecture:** All 8 picks are independent, single-PR-each, parallel-safe. Each ships through TDD with isolated worktrees so two implementers can run in parallel without collision. Reuses existing OC primitives (`PreLLMCall` hook, `outgoing_queue`, `BaseChannelAdapter`, `ToolRegistry`, F1 ConsentGate) — no new SDK lifecycle events.

**Tech Stack:** Python 3.12+, pydantic v2 (existing), pytest (existing), Typer (existing), httpx (existing). Zero new dependencies.

**Spec:** `docs/superpowers/specs/2026-04-28-openclaw-tier1-port-design.md` (commit `5db13924`).

**Pick map:**

- **Sub-project A** — Block streaming chunker + `humanDelay` (M, ~2d)
- **Sub-project B** — Active Memory pre-reply sub-agent (M, ~2d) — uses existing `PreLLMCall` + `HookDecision.modified_message`
- **Sub-project C** — Anti-loop / repetition detector (S, ~1d)
- **Sub-project D** — Replay sanitization (S, ~half day)
- **Sub-project E** — Auth profile rotation cooldown + auto-monitor (S, ~1d)
- **Sub-project F** — Sessions-* tools (5 tools, M, ~2-3d)
- **Sub-project G** — Clarify_tool (S, ~half day)
- **Sub-project H** — Send_message_tool (S, ~half day)

**Out of scope (rationale in spec §8):** Multi-agent isolation, Standing Orders DSL, hook taxonomy expansion, inbound queue modes, cron isolation modes, Lobster, TaskFlow, OTEL, Heartbeat, OSC8, sandbox-browser, memory-lancedb/wiki, mcporter, multi-stage approval rendering (web), 50-provider/25-channel long tail, ACP bridge expansion, CLAUDE.md §5 won't-do.

---

## Phase 0 — Pre-flight verification (run BEFORE any sub-project)

**Why:** Hermes plan caught 9 critical assumption-breaks at Phase 0. Same risk here. Don't skip.

**Branch for Phase 0:** `prep/openclaw-tier1-decisions` — docs only; no PR needed; the doc is the contract.

### Task 0.1: Verify HookDecision.modified_message is honored for PreLLMCall

**Why:** Sub-project B reuses the existing `HookDecision.modified_message` channel to inject `<relevant-memories>` as a system reminder. We need to confirm the hook engine actually injects it for PRE_LLM_CALL events (it was originally designed for PreToolUse).

**Files:**
- Read: `opencomputer/hooks/engine.py`
- Read: `opencomputer/agent/loop.py` (look at where `PRE_LLM_CALL` is emitted and what it does with the result)

- [ ] **Step 1:** Read `opencomputer/hooks/engine.py` end-to-end. Document how `HookEngine.emit(event, ctx)` collects HookDecisions and what callers do with them.

- [ ] **Step 2:** Read `opencomputer/agent/loop.py` — find the line where `PRE_LLM_CALL` is emitted (likely just before `provider.complete(messages)`). Document what happens with the returned HookDecisions: are `modified_message` strings injected into messages?

- [ ] **Step 3:** Document findings to `docs/superpowers/plans/2026-04-28-openclaw-tier1-DECISIONS.md` under "0.1 PreLLMCall + modified_message". Two outcomes:
  - **Outcome A:** modified_message is already injected → Sub-project B can use it as-is.
  - **Outcome B:** modified_message is not injected for PreLLMCall → Sub-project B must add 5-10 lines to `agent/loop.py` to inject it (or to `hooks/engine.py` to emit a transformed message list). This is a single-task addition, not a blocker.

- [ ] **Step 4:** Commit: `git add docs/superpowers/plans/2026-04-28-openclaw-tier1-DECISIONS.md && git commit -m "docs(openclaw): Phase 0.1 — PreLLMCall modified_message injection verified"`.

### Task 0.2: Verify channel-adapter streaming surface

**Why:** Sub-project A wraps `on_delta` callbacks. We need to confirm where streaming deltas land in each channel adapter.

**Files:**
- Read: `extensions/telegram/adapter.py`, `extensions/discord/adapter.py`, `extensions/slack/adapter.py`
- Read: `opencomputer/gateway/dispatch.py` (where `on_delta` is dispatched)

- [ ] **Step 1:** In `dispatch.py`, find where streaming deltas are dispatched to adapters. Search for `on_delta`, `stream_delta`, or `edit_message`. Document the function signature.

- [ ] **Step 2:** In each of the three adapters (telegram, discord, slack), find the function that receives a delta and calls `adapter.send()` or `adapter.edit_message()`. Note the function name and call shape.

- [ ] **Step 3:** Document in `DECISIONS.md` § "0.2 Channel streaming surface": exact integration point for the chunker. Two options to record:
  - **Option α:** Wrap inside dispatch.py before calling adapter (one place to change).
  - **Option β:** Add a helper to BaseChannelAdapter and call it inside each adapter's send/edit path (per-adapter).
  - Pick whichever matches the existing pattern. Document the pick.

- [ ] **Step 4:** Commit.

### Task 0.3: Verify SessionDB API for sessions-* tools

**Files:**
- Read: `opencomputer/agent/state.py::SessionDB`

- [ ] **Step 1:** List all public methods on SessionDB (look for `def ` not starting with `_`). Note signatures of: `create_session`, `list_sessions`, `get_messages`, `get_session_summary`, `mark_inactive` (or equivalents).

- [ ] **Step 2:** Confirm whether SessionDB supports cross-process access (i.e., one Gateway process writes; the agent loop in another process can read live updates) — check for SQLite WAL mode in the connect call.

- [ ] **Step 3:** Document in `DECISIONS.md` § "0.3 SessionDB API for Sessions-* tools": exact method names + signatures the 5 tools will call.

- [ ] **Step 4:** Commit.

### Task 0.4: Verify outgoing_queue API for send_message_tool

**Files:**
- Read: `plugin_sdk/outgoing_queue.py` (or equivalent)
- Read: where `outgoing_queue.put_send` (or similar) is called

- [ ] **Step 1:** Find the outgoing-queue module. Document the public API: `put_send(channel, peer, message)` exact signature, `get_send()` consumer side, where it's bound on `PluginAPI`.

- [ ] **Step 2:** Document in `DECISIONS.md` § "0.4 outgoing_queue API for SendMessageTool": exact call shape the tool will use.

- [ ] **Step 3:** Commit.

### Task 0.5: Verify provider client error surface for cooldown

**Files:**
- Read: `extensions/anthropic-provider/provider.py`, `extensions/openai-provider/provider.py`
- Read: any existing `credential_pool.py`

- [ ] **Step 1:** Find the credential-pool rotation point. Document on what error types it currently rotates (probably 401/403). Note the exception types thrown by the SDK clients (`anthropic.APIStatusError`, `openai.APIStatusError`).

- [ ] **Step 2:** Document in `DECISIONS.md` § "0.5 Provider error surface": exact exceptions the cooldown logic will catch + retry classification (transient 5xx/timeout vs permanent 401/403).

- [ ] **Step 3:** Commit and push the prep branch: `git push -u origin prep/openclaw-tier1-decisions`.

### Task 0.6: F1 ConsentGate integration shape

**Files:**
- Read: `plugin_sdk/consent.py`, `opencomputer/consent/gate.py` (or equivalent)
- Read: existing tool that uses `capability_claims` (e.g., a coding-harness tool)

- [ ] **Step 1:** Find a `BaseTool` subclass with `capability_claims` set. Document the exact attribute (is it `capability_claims: tuple[str, ...]` on the class?).

- [ ] **Step 2:** Document in `DECISIONS.md` § "0.6 F1 capability claims for new tools": the literal claim names Sub-projects F + H will use:
  - F: `sessions.spawn`, `sessions.send`, `sessions.list`, `sessions.history`, `sessions.status`
  - H: `messaging.send.<channel>` — confirm whether claim names use dotted path with channel suffix.
  - G (Clarify): no capability claim needed (uses existing AskUserQuestion machinery).

- [ ] **Step 3:** Commit.

**Phase 0 commit gate:** `DECISIONS.md` is complete with 6 sections, committed, pushed. No blockers identified or all blockers documented as added tasks. Only then proceed to Sub-projects.

---

## Sub-project A — Block streaming chunker + `humanDelay`

**Goal:** Replace robotic raw-delta streaming on channel adapters with paragraph-aware chunking + randomized humanDelay between blocks.

**Branch:** `feat/openclaw-1a-block-chunker` (from `main` post-Phase 0).

**Files:**
- Create: `plugin_sdk/streaming/__init__.py`
- Create: `plugin_sdk/streaming/block_chunker.py`
- Create: `tests/streaming/__init__.py`
- Create: `tests/streaming/test_block_chunker.py`
- Create: `tests/streaming/test_block_chunker_integration_telegram.py`
- Modify: `plugin_sdk/__init__.py` — export `BlockChunker`, `Block`
- Modify: `plugin_sdk/channel_contract.py` — add `BaseChannelAdapter._maybe_chunk_delta` helper
- Modify: per Phase 0.2 finding — either `gateway/dispatch.py` (Option α) or `extensions/{telegram,discord,slack,matrix,mattermost}/adapter.py` (Option β)

### Task A1: Define `Block` dataclass + `BlockChunker.feed/flush/human_delay`

**Files:**
- Create: `plugin_sdk/streaming/block_chunker.py`
- Test: `tests/streaming/test_block_chunker.py`

- [ ] **Step 1: Write the failing test for paragraph-first split**

```python
# tests/streaming/test_block_chunker.py
"""Tests for BlockChunker — boundary-aware streaming chunker."""
from __future__ import annotations

import random

import pytest

from plugin_sdk.streaming import BlockChunker, Block


def test_paragraph_first_split():
    chunker = BlockChunker(min_chars=10, max_chars=1000)
    out = chunker.feed("First paragraph. This is line two.\n\nSecond paragraph here.\n\n")
    assert len(out) == 2
    assert out[0].text == "First paragraph. This is line two."
    assert out[1].text == "Second paragraph here."


def test_min_chars_buffers_short_input():
    chunker = BlockChunker(min_chars=80, max_chars=1500)
    out = chunker.feed("short.")
    assert out == []
    out = chunker.flush()
    assert len(out) == 1
    assert out[0].text == "short."


def test_max_chars_force_split():
    text = "x" * 2000
    chunker = BlockChunker(min_chars=10, max_chars=1500)
    out = chunker.feed(text)
    assert len(out) >= 1
    assert all(len(b.text) <= 1500 for b in out)


def test_never_splits_inside_fence():
    text = "Before fence.\n\n```python\nlong " + ("x" * 1000) + " line\n```\n\nAfter."
    chunker = BlockChunker(min_chars=10, max_chars=200)
    out = chunker.feed(text)
    flushed = chunker.flush()
    all_blocks = out + flushed
    joined = "".join(b.text for b in all_blocks)
    # Reconstruction preserves the fence intact (no mid-fence split)
    assert "```python\nlong " in joined
    assert joined.endswith("After.")


def test_human_delay_within_range():
    random.seed(42)
    chunker = BlockChunker(min_chars=10, max_chars=1500, human_delay_min_ms=800, human_delay_max_ms=2500)
    delays = [chunker.human_delay() for _ in range(100)]
    assert all(0.8 <= d <= 2.5 for d in delays)


def test_block_dataclass_is_immutable():
    b = Block(text="hello", boundary="paragraph")
    with pytest.raises(Exception):
        b.text = "mutated"  # frozen dataclass should reject
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/streaming/test_block_chunker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'plugin_sdk.streaming'`.

- [ ] **Step 3: Implement BlockChunker**

```python
# plugin_sdk/streaming/__init__.py
"""Streaming primitives for channel adapters.

Block-aware chunker that emits paragraph/newline/sentence-bounded blocks
from raw provider stream-deltas. Mirrors OpenClaw's streaming.md pattern
to avoid robotic per-token edits on Telegram/Discord/Slack/etc.

Public API: ``BlockChunker``, ``Block``.
"""
from __future__ import annotations

from plugin_sdk.streaming.block_chunker import Block, BlockChunker

__all__ = ["Block", "BlockChunker"]
```

```python
# plugin_sdk/streaming/block_chunker.py
"""Block-aware streaming chunker. Standalone — depends only on stdlib."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

BoundaryKind = Literal["paragraph", "newline", "sentence", "whitespace", "max"]
_FENCE = "```"
_PARAGRAPH = "\n\n"
_NEWLINE = "\n"
_SENTENCE_TERMINATORS = (". ", "? ", "! ")


@dataclass(frozen=True, slots=True)
class Block:
    """One ready-to-deliver unit emitted by the chunker."""
    text: str
    boundary: BoundaryKind


class BlockChunker:
    """Buffers stream deltas; emits blocks at natural boundaries.

    Boundary priority: paragraph → newline → sentence → whitespace → max.
    Never splits inside a fenced code block (``\\`\\`\\``).

    Args:
        min_chars: blocks below this are buffered until larger.
        max_chars: blocks above this are split at the highest-priority boundary.
        idle_coalesce_ms: deltas arriving within this window merge.
        human_delay_min_ms / human_delay_max_ms: random pause between blocks.
    """

    def __init__(
        self,
        min_chars: int = 80,
        max_chars: int = 1500,
        idle_coalesce_ms: int = 100,
        human_delay_min_ms: int = 800,
        human_delay_max_ms: int = 2500,
    ) -> None:
        if min_chars < 1 or max_chars < min_chars:
            raise ValueError(f"invalid min/max: {min_chars}/{max_chars}")
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.idle_coalesce_ms = idle_coalesce_ms
        self.human_delay_min_ms = human_delay_min_ms
        self.human_delay_max_ms = human_delay_max_ms
        self._buf: str = ""

    def feed(self, delta: str) -> list[Block]:
        """Append delta to buffer; return blocks ready to emit."""
        self._buf += delta
        out: list[Block] = []
        while True:
            block = self._extract_one()
            if block is None:
                break
            out.append(block)
        return out

    def flush(self) -> list[Block]:
        """Emit whatever remains in the buffer, regardless of size."""
        if not self._buf:
            return []
        # Drain whatever's left as a final block; pick best boundary if possible.
        text = self._buf
        self._buf = ""
        return [Block(text=text, boundary="max")]

    def human_delay(self) -> float:
        """Random pause in seconds between block deliveries."""
        ms = random.uniform(self.human_delay_min_ms, self.human_delay_max_ms)
        return ms / 1000.0

    # --- internals -------------------------------------------------------

    def _extract_one(self) -> Block | None:
        """Try to peel one block off the front of the buffer. None = wait."""
        buf = self._buf
        if len(buf) < self.min_chars:
            return None

        # If buffer too long, force-split at best boundary <= max_chars.
        if len(buf) > self.max_chars:
            return self._force_split()

        # Try natural boundaries in priority order.
        # 1. paragraph
        idx = self._find_boundary(buf, _PARAGRAPH, fence_safe=True)
        if idx is not None and idx >= self.min_chars:
            return self._consume(idx, len(_PARAGRAPH), "paragraph")

        # 2. newline
        idx = self._find_boundary(buf, _NEWLINE, fence_safe=True)
        if idx is not None and idx >= self.min_chars:
            return self._consume(idx, len(_NEWLINE), "newline")

        # 3. sentence
        for term in _SENTENCE_TERMINATORS:
            idx = self._find_boundary(buf, term, fence_safe=True)
            if idx is not None and idx >= self.min_chars:
                return self._consume(idx + len(term) - 1, 1, "sentence")
        return None

    def _force_split(self) -> Block:
        """Buffer exceeds max_chars; split at best boundary <= max_chars."""
        buf = self._buf
        cap = self.max_chars
        for sep, kind in (
            (_PARAGRAPH, "paragraph"),
            (_NEWLINE, "newline"),
        ):
            idx = self._find_boundary(buf[:cap], sep, fence_safe=True)
            if idx is not None and idx >= self.min_chars:
                return self._consume(idx, len(sep), kind)
        for term in _SENTENCE_TERMINATORS:
            idx = self._find_boundary(buf[:cap], term, fence_safe=True)
            if idx is not None and idx >= self.min_chars:
                return self._consume(idx + len(term) - 1, 1, "sentence")
        # No boundary found within cap; split at last whitespace before cap.
        idx = buf.rfind(" ", self.min_chars, cap)
        if idx > 0 and not self._inside_fence(buf, idx):
            return self._consume(idx, 1, "whitespace")
        # Worst case: hard cut at cap (still respecting fences).
        idx = self._latest_safe_cut(buf, cap)
        return self._consume(idx, 0, "max")

    def _find_boundary(self, buf: str, sep: str, *, fence_safe: bool) -> int | None:
        """Return the smallest idx ≥ min_chars where ``buf[idx:idx+len(sep)] == sep``,
        or None. If fence_safe, skip any idx that lies inside a code fence."""
        start = self.min_chars
        while True:
            idx = buf.find(sep, start)
            if idx == -1:
                return None
            if not fence_safe or not self._inside_fence(buf, idx):
                return idx
            start = idx + 1

    def _inside_fence(self, buf: str, idx: int) -> bool:
        """True iff position idx is inside an unclosed ``` fence in buf[:idx]."""
        prefix = buf[:idx]
        return prefix.count(_FENCE) % 2 == 1

    def _latest_safe_cut(self, buf: str, cap: int) -> int:
        """Find the latest position ≤ cap that doesn't fall inside a fence."""
        for i in range(min(cap, len(buf)), self.min_chars, -1):
            if not self._inside_fence(buf, i):
                return i
        return self.min_chars

    def _consume(self, length: int, sep_len: int, kind: BoundaryKind) -> Block:
        """Pop ``length`` chars off the front, plus ``sep_len`` separator chars."""
        text = self._buf[:length].rstrip()
        self._buf = self._buf[length + sep_len:].lstrip()
        return Block(text=text, boundary=kind)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/streaming/test_block_chunker.py -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add plugin_sdk/streaming/ tests/streaming/__init__.py tests/streaming/test_block_chunker.py
git commit -m "feat(streaming): BlockChunker — paragraph-aware streaming chunker (1.A core)

OpenClaw-style chunker that buffers raw provider stream-deltas and emits
blocks at natural boundaries (paragraph → newline → sentence → whitespace
→ max). Never splits inside fenced code blocks. Adds humanDelay() for
randomized between-block pauses (default 800-2500ms).

Standalone — only stdlib dependencies. Wraps in channel adapters via
follow-up tasks (A2: SDK exports, A3: BaseChannelAdapter wrapper, A4:
adapter integration, A5: config).

Test surface: 6 unit tests covering paragraph-first, min-chars buffering,
max-chars force-split, fence-protected split, human_delay range, immutable
Block dataclass."
```

### Task A2: Export from plugin_sdk + add SDK config types

**Files:**
- Modify: `plugin_sdk/__init__.py`

- [ ] **Step 1:** Add to `plugin_sdk/__init__.py`:

```python
# After existing imports
from plugin_sdk.streaming import Block, BlockChunker
```

And add to the `__all__` tuple:

```python
__all__ = (
    # ... existing entries
    "Block",
    "BlockChunker",
)
```

- [ ] **Step 2:** Run plugin_sdk boundary test:

```bash
pytest tests/test_phase6a.py -v -k plugin_sdk
```

Expected: PASS (boundary preserved — streaming module has no `from opencomputer` imports).

- [ ] **Step 3: Commit**

```bash
git add plugin_sdk/__init__.py
git commit -m "feat(streaming): export Block + BlockChunker from plugin_sdk"
```

### Task A3: BaseChannelAdapter helper `_maybe_chunk_delta`

**Files:**
- Modify: `plugin_sdk/channel_contract.py`
- Test: `tests/streaming/test_block_chunker.py` — add helper test

- [ ] **Step 1: Write the failing test for the wrapper helper**

Append to `tests/streaming/test_block_chunker.py`:

```python
import asyncio
import pytest


@pytest.mark.asyncio
async def test_base_channel_adapter_maybe_chunk_delta_wraps_when_enabled(monkeypatch):
    from plugin_sdk.channel_contract import BaseChannelAdapter

    class _StubAdapter(BaseChannelAdapter):
        platform = "test"

        def __init__(self) -> None:
            super().__init__()
            self.streaming_block_chunker = True
            self._chunker = BlockChunker(min_chars=10, max_chars=200,
                                         human_delay_min_ms=0, human_delay_max_ms=0)
            self.sent: list[str] = []

        async def send(self, chat_id, text, **kw):
            self.sent.append(text)
            return None

        async def edit_message(self, *_a, **_kw): ...
        async def send_typing(self, *_a, **_kw): ...
        async def connect(self): ...
        async def disconnect(self): ...

    a = _StubAdapter()
    async for _block in a._maybe_chunk_delta("Hello world.\n\nSecond para here.\n\n", chat_id="c1"):
        pass
    assert a.sent == ["Hello world.", "Second para here."]


@pytest.mark.asyncio
async def test_base_channel_adapter_maybe_chunk_delta_passthrough_when_disabled():
    from plugin_sdk.channel_contract import BaseChannelAdapter

    class _StubAdapter(BaseChannelAdapter):
        platform = "test"

        def __init__(self) -> None:
            super().__init__()
            self.streaming_block_chunker = False
            self.sent: list[str] = []

        async def send(self, chat_id, text, **kw):
            self.sent.append(text)
            return None

        async def edit_message(self, *_a, **_kw): ...
        async def send_typing(self, *_a, **_kw): ...
        async def connect(self): ...
        async def disconnect(self): ...

    a = _StubAdapter()
    async for _ in a._maybe_chunk_delta("raw delta passthrough", chat_id="c1"):
        pass
    assert a.sent == ["raw delta passthrough"]
```

- [ ] **Step 2: Run test → fail**

Run: `pytest tests/streaming/test_block_chunker.py -v -k maybe_chunk`
Expected: FAIL — `_maybe_chunk_delta` not defined.

- [ ] **Step 3: Add helper to BaseChannelAdapter**

In `plugin_sdk/channel_contract.py`, add (near the bottom of `class BaseChannelAdapter`):

```python
async def _maybe_chunk_delta(self, delta: str, *, chat_id: str):
    """If chunker is enabled, deliver delta as natural blocks with humanDelay.
    Else, pass-through to a single send() call.

    Yields nothing meaningful — yields ``None`` after each emitted block so
    callers can choose to await typing-indicators or other side effects.
    """
    import asyncio
    from plugin_sdk.streaming import BlockChunker

    if not getattr(self, "streaming_block_chunker", False):
        await self.send(chat_id, delta)
        yield None
        return

    chunker = getattr(self, "_chunker", None)
    if chunker is None:
        chunker = BlockChunker()
        self._chunker = chunker

    for block in chunker.feed(delta):
        await self.send(chat_id, block.text)
        delay = chunker.human_delay()
        if delay > 0:
            await asyncio.sleep(delay)
        yield None

async def _flush_chunker(self, *, chat_id: str) -> None:
    """End-of-stream — drain any buffered text. Call from on_stream_end."""
    chunker = getattr(self, "_chunker", None)
    if chunker is None:
        return
    for block in chunker.flush():
        await self.send(chat_id, block.text)
    self._chunker = None
```

- [ ] **Step 4: Run test → pass**

Run: `pytest tests/streaming/test_block_chunker.py -v -k maybe_chunk`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add plugin_sdk/channel_contract.py tests/streaming/test_block_chunker.py
git commit -m "feat(streaming): BaseChannelAdapter._maybe_chunk_delta helper

Adapters opt in by setting self.streaming_block_chunker=True (loaded from
config). Wrapper handles chunker lifecycle (feed → emit → humanDelay) and
falls through to raw send() when disabled. Per-channel _flush_chunker for
end-of-stream drain."
```

### Task A4: Wire chunker into adapter streaming path (per Phase 0.2 finding)

**Files:** Per Phase 0.2 — either `gateway/dispatch.py` or per-adapter. Assume Option α (dispatch-level) — the cleaner pattern.

- [ ] **Step 1: Read the streaming delta dispatch in gateway/dispatch.py**

Locate the function that calls `adapter.edit_message()` or `adapter.send()` for each provider stream delta. Document its exact signature.

- [ ] **Step 2: Wrap the delta call with `_maybe_chunk_delta`**

Replace the direct `adapter.send(chat_id, text_so_far)` (or `edit_message(...)`) with:

```python
# In gateway/dispatch.py — wherever provider deltas are dispatched to adapter
async for _ in adapter._maybe_chunk_delta(delta, chat_id=chat_id):
    pass  # blocks already sent inside helper
```

For end-of-stream (when provider.stream() finishes):

```python
await adapter._flush_chunker(chat_id=chat_id)
```

- [ ] **Step 3: Add integration test**

```python
# tests/streaming/test_block_chunker_integration_telegram.py
"""Integration test — telegram adapter with chunker enabled emits human-paced blocks."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_telegram_adapter_chunker_emits_paragraph_blocks(monkeypatch):
    """When streaming_block_chunker is enabled, multi-paragraph deltas are
    delivered as separate send() calls."""
    from plugin_sdk.streaming import BlockChunker

    sent_calls: list[str] = []

    class _FakeTelegramAdapter:
        platform = "telegram"
        streaming_block_chunker = True
        _chunker = BlockChunker(
            min_chars=10, max_chars=200,
            human_delay_min_ms=0, human_delay_max_ms=0,
        )
        async def send(self, chat_id, text, **kw):
            sent_calls.append(text)

        async def _maybe_chunk_delta(self, delta, *, chat_id):
            from plugin_sdk.channel_contract import BaseChannelAdapter
            async for x in BaseChannelAdapter._maybe_chunk_delta(
                self, delta, chat_id=chat_id
            ):
                yield x

    a = _FakeTelegramAdapter()
    async for _ in a._maybe_chunk_delta(
        "First paragraph here.\n\nSecond paragraph here.\n\n",
        chat_id="t1",
    ):
        pass
    assert sent_calls == ["First paragraph here.", "Second paragraph here."]
```

- [ ] **Step 4: Run integration test**

Run: `pytest tests/streaming/test_block_chunker_integration_telegram.py -v`
Expected: PASS.

- [ ] **Step 5: Run full streaming test suite + regression**

Run: `pytest tests/streaming/ tests/test_dispatch.py tests/test_telegram_adapter.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/gateway/dispatch.py tests/streaming/test_block_chunker_integration_telegram.py
git commit -m "feat(streaming): wire BlockChunker into gateway delta dispatch

Per-adapter opt-in via streaming_block_chunker flag. Falls through to raw
send() when disabled (default for TUI; user-opted ON for messaging
channels). Drain on stream-end via _flush_chunker."
```

### Task A5: Config schema + per-channel opt-in

**Files:**
- Modify: `opencomputer/agent/config.py` (or wherever channel config is loaded)
- Modify: each enabled channel adapter (telegram/discord/slack/matrix/mattermost) to read flag

- [ ] **Step 1: Locate channel-config loading**

Find where adapters read their per-channel YAML config (likely `opencomputer/agent/config.py::load_channel_config` or similar). Document the loading function.

- [ ] **Step 2: Add `streaming.block_chunker` parsing**

Add to the channel-config loader:

```python
streaming_cfg = raw.get("streaming", {})
adapter_kwargs["streaming_block_chunker"] = bool(streaming_cfg.get("block_chunker", False))
adapter_kwargs["streaming_block_chunker_min_chars"] = int(streaming_cfg.get("min_chars", 80))
adapter_kwargs["streaming_block_chunker_max_chars"] = int(streaming_cfg.get("max_chars", 1500))
adapter_kwargs["streaming_human_delay_min_ms"] = int(streaming_cfg.get("human_delay_min_ms", 800))
adapter_kwargs["streaming_human_delay_max_ms"] = int(streaming_cfg.get("human_delay_max_ms", 2500))
```

In each adapter's `__init__`, accept these kwargs and pass them to `BlockChunker(...)` when first creating the chunker (lazy in `_maybe_chunk_delta`).

- [ ] **Step 3: Test config round-trip**

```python
# tests/streaming/test_block_chunker.py — append
def test_channel_config_round_trip(tmp_path):
    """YAML config block_chunker:true loads onto adapter."""
    import yaml
    from opencomputer.agent.config import load_channel_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "channels": {
            "telegram": {
                "streaming": {
                    "block_chunker": True,
                    "min_chars": 100,
                    "human_delay_min_ms": 500,
                }
            }
        }
    }))
    cfg = load_channel_config(cfg_path, "telegram")
    assert cfg["streaming_block_chunker"] is True
    assert cfg["streaming_block_chunker_min_chars"] == 100
    assert cfg["streaming_human_delay_min_ms"] == 500
```

- [ ] **Step 4: Run test**

Run: `pytest tests/streaming/test_block_chunker.py::test_channel_config_round_trip -v`
Expected: PASS.

- [ ] **Step 5: Commit + push branch**

```bash
git add opencomputer/agent/config.py tests/streaming/test_block_chunker.py
git commit -m "feat(streaming): per-channel config opt-in for BlockChunker

YAML schema:
  channels:
    telegram:
      streaming:
        block_chunker: true
        min_chars: 80
        max_chars: 1500
        human_delay_min_ms: 800
        human_delay_max_ms: 2500

Default OFF for backwards-compat; user opts in per-channel."

git push -u origin feat/openclaw-1a-block-chunker
```

- [ ] **Step 6: Open PR**

```bash
gh pr create --title "feat(streaming): BlockChunker + humanDelay (OpenClaw 1.A)" --body "$(cat <<'EOF'
## Summary
- Adds `plugin_sdk/streaming/BlockChunker` — paragraph-aware streaming chunker for channel adapters.
- Adds `BaseChannelAdapter._maybe_chunk_delta` wrapper.
- Wires into `gateway/dispatch.py` delta path.
- Per-channel YAML opt-in (default OFF).

## Why
Today: provider tokens stream straight to adapter `send()` → robotic per-token Telegram edits.
After: adapters opt in → paragraph-bounded blocks with random 800-2500ms pauses → human-paced delivery.

Plan: `docs/superpowers/plans/2026-04-28-openclaw-tier1-port.md` Sub-project A.

## Test plan
- [x] Unit tests (6) — paragraph/newline/sentence boundaries, fence-safe split, humanDelay range
- [x] Integration test — Telegram adapter delivers paragraph-by-paragraph with chunker on
- [x] Regression — existing dispatch + telegram tests pass with chunker off

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Sub-project B — Active Memory pre-reply sub-agent

**Goal:** Bounded sub-agent fires on every eligible reply, queries memory, optionally injects relevant memories as a system reminder via the existing `HookDecision.modified_message` channel.

**Branch:** `feat/openclaw-1b-active-memory` (from `main`).

**Architecture decision (per Phase 0.1):** Use existing `HookDecision.modified_message` to inject the `<relevant-memories>` block. **No SDK change.** If Phase 0.1 finds modified_message isn't honored for PRE_LLM_CALL, this sub-project's first task is to honor it (5-10 LOC in `agent/loop.py`).

**Files:**
- Create: `extensions/active-memory/__init__.py`
- Create: `extensions/active-memory/plugin.py`
- Create: `extensions/active-memory/plugin.json` (manifest, OpenClaw-style)
- Create: `extensions/active-memory/runtime.py` (sub-agent runner)
- Create: `extensions/active-memory/cache.py` (decision cache)
- Create: `extensions/active-memory/prompts/balanced.j2`
- Create: `extensions/active-memory/prompts/strict.j2`
- Create: `extensions/active-memory/prompts/contextual.j2`
- Create: `extensions/active-memory/prompts/recall_heavy.j2`
- Create: `extensions/active-memory/prompts/precision_heavy.j2`
- Create: `extensions/active-memory/prompts/preference_only.j2`
- Create: `extensions/active-memory/slash_commands.py`
- Create: `tests/active_memory/__init__.py`
- Create: `tests/active_memory/test_plugin.py`
- Create: `tests/active_memory/test_runtime.py`
- Create: `tests/active_memory/test_cache.py`
- Modify (only if Phase 0.1 Outcome B): `opencomputer/agent/loop.py` — honor modified_message for PRE_LLM_CALL

### Task B1: Decision cache `(chat_id, last_user_msg_hash) -> decision`

**Files:**
- Create: `extensions/active-memory/cache.py`
- Test: `tests/active_memory/test_cache.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/active_memory/test_cache.py
"""Tests for active-memory decision cache."""
from __future__ import annotations

import time

import pytest

# Importable through the plugin's local module — engineer to use sys.path or pytest conftest
# to point at extensions/active-memory/ during tests. Plugin tests use the standard
# OC plugin-loading test fixtures from tests/conftest.py.

@pytest.fixture
def cache(tmp_path, monkeypatch):
    import sys
    monkeypatch.syspath_prepend("extensions/active-memory")
    from cache import DecisionCache
    return DecisionCache(ttl_ms=1000)


def test_cache_hit_returns_prior_decision(cache):
    cache.put("chat-1", "hash-A", {"action": "inject", "summary": "X"})
    got = cache.get("chat-1", "hash-A")
    assert got == {"action": "inject", "summary": "X"}


def test_cache_miss_returns_none(cache):
    assert cache.get("chat-1", "missing-hash") is None


def test_cache_expires_after_ttl(cache, monkeypatch):
    cache.put("chat-1", "hash-A", {"action": "skip"})
    # advance time past ttl
    monkeypatch.setattr(time, "monotonic", lambda: time.monotonic() + 2.0)
    assert cache.get("chat-1", "hash-A") is None


def test_cache_distinct_chats_isolated(cache):
    cache.put("chat-1", "hash-X", {"action": "inject", "summary": "1"})
    cache.put("chat-2", "hash-X", {"action": "skip"})
    assert cache.get("chat-1", "hash-X")["action"] == "inject"
    assert cache.get("chat-2", "hash-X")["action"] == "skip"
```

- [ ] **Step 2: Implement cache**

```python
# extensions/active-memory/cache.py
"""LRU-bounded TTL cache for active-memory pre-reply decisions.

Key: ``(chat_id, last_user_msg_hash)``. Value: ``{"action": "inject"|"skip",
"summary": str}``. Entries expire after ``ttl_ms`` from put-time.

Pure stdlib. No SDK imports — this module is plugin-local.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

CacheKey = tuple[str, str]


class DecisionCache:
    """OrderedDict-backed LRU+TTL cache."""

    def __init__(self, ttl_ms: int = 60_000, max_entries: int = 256) -> None:
        self.ttl_s = ttl_ms / 1000.0
        self.max_entries = max_entries
        self._data: OrderedDict[CacheKey, tuple[float, dict[str, Any]]] = OrderedDict()

    def get(self, chat_id: str, msg_hash: str) -> dict[str, Any] | None:
        key = (chat_id, msg_hash)
        if key not in self._data:
            return None
        ts, value = self._data[key]
        if time.monotonic() - ts > self.ttl_s:
            del self._data[key]
            return None
        # LRU bump
        self._data.move_to_end(key)
        return value

    def put(self, chat_id: str, msg_hash: str, decision: dict[str, Any]) -> None:
        key = (chat_id, msg_hash)
        self._data[key] = (time.monotonic(), decision)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)
```

- [ ] **Step 3: Run tests → pass**

Run: `pytest tests/active_memory/test_cache.py -v`
Expected: 4 PASS.

- [ ] **Step 4: Commit**

```bash
git add extensions/active-memory/cache.py tests/active_memory/__init__.py tests/active_memory/test_cache.py
git commit -m "feat(active-memory): TTL+LRU decision cache (1.B step 1)

Key: (chat_id, last_user_msg_hash). Value: {action, summary}. Default
ttl 60s, max 256 entries. Pure stdlib."
```

### Task B2: Sub-agent runner — bounded recall loop

**Files:**
- Create: `extensions/active-memory/runtime.py`
- Create: `extensions/active-memory/prompts/balanced.j2` (and 5 others — ship balanced first; others copy-edited variants in Task B6)
- Test: `tests/active_memory/test_runtime.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/active_memory/test_runtime.py
"""Tests for active-memory bounded sub-agent runner."""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def runner(monkeypatch, tmp_path):
    import sys
    monkeypatch.syspath_prepend("extensions/active-memory")
    from runtime import ActiveMemoryRunner

    class _StubProvider:
        async def complete(self, *, model, messages, tools=None, max_tokens=None):
            return type("R", (), {"text": '{"action":"skip","summary":""}', "tool_calls": []})()

    class _StubMemory:
        def memory_search(self, query, limit): return []
        def memory_get(self, id): return None

    return ActiveMemoryRunner(
        provider=_StubProvider(),
        memory=_StubMemory(),
        model="gpt-4o-mini",
        timeout_ms=2000,
        prompt_style="balanced",
    )


@pytest.mark.asyncio
async def test_subagent_returns_decision_dict(runner):
    out = await runner.run(chat_id="c1", recent_messages=[
        {"role": "user", "content": "hello"},
    ])
    assert out["action"] in ("inject", "skip")


@pytest.mark.asyncio
async def test_subagent_fail_open_on_timeout(runner, monkeypatch):
    async def slow(*a, **kw):
        await asyncio.sleep(5)
        return None
    monkeypatch.setattr(runner.provider, "complete", slow)
    runner.timeout_ms = 100
    out = await runner.run(chat_id="c1", recent_messages=[{"role": "user", "content": "x"}])
    assert out == {"action": "skip", "summary": ""}


@pytest.mark.asyncio
async def test_subagent_invalid_json_falls_back_to_skip(runner, monkeypatch):
    async def bad(*a, **kw):
        return type("R", (), {"text": "not json at all", "tool_calls": []})()
    monkeypatch.setattr(runner.provider, "complete", bad)
    out = await runner.run(chat_id="c1", recent_messages=[{"role": "user", "content": "x"}])
    assert out == {"action": "skip", "summary": ""}
```

- [ ] **Step 2: Implement runtime**

```python
# extensions/active-memory/runtime.py
"""Bounded pre-reply sub-agent runner.

Hooks into ``HookEvent.PRE_LLM_CALL``. Calls a small/cheap provider with a
prompt-style template, expects a JSON response of the form::

    {"action": "inject"|"skip", "summary": "<text or empty>"}

On 'inject', the calling plugin returns the rendered ``<relevant-memories>``
block via HookDecision.modified_message — the agent loop injects it as a
system reminder before the upcoming provider.complete() call.

Bounded by ``timeout_ms``. Fail-open on any exception: returns
``{"action": "skip", "summary": ""}`` so the main reply proceeds unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent / "prompts"
_FAIL_OPEN: dict[str, Any] = {"action": "skip", "summary": ""}


class ActiveMemoryRunner:
    def __init__(
        self,
        *,
        provider: Any,
        memory: Any,
        model: str,
        timeout_ms: int = 8000,
        prompt_style: str = "balanced",
        recent_user_turns: int = 3,
        recent_assistant_turns: int = 1,
    ) -> None:
        self.provider = provider
        self.memory = memory
        self.model = model
        self.timeout_ms = timeout_ms
        self.prompt_style = prompt_style
        self.recent_user_turns = recent_user_turns
        self.recent_assistant_turns = recent_assistant_turns

    async def run(self, *, chat_id: str, recent_messages: list[dict]) -> dict:
        try:
            return await asyncio.wait_for(
                self._invoke(chat_id, recent_messages),
                timeout=self.timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            logger.info("active-memory: timeout — fail-open")
            return _FAIL_OPEN
        except Exception:
            logger.exception("active-memory: error — fail-open")
            return _FAIL_OPEN

    async def _invoke(self, chat_id: str, recent_messages: list[dict]) -> dict:
        prompt = self._render_prompt(recent_messages)
        result = await self.provider.complete(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
        )
        text = getattr(result, "text", "") or ""
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return _FAIL_OPEN
        if not isinstance(obj, dict) or obj.get("action") not in ("inject", "skip"):
            return _FAIL_OPEN
        obj.setdefault("summary", "")
        return obj

    def _render_prompt(self, recent_messages: list[dict]) -> str:
        path = PROMPT_DIR / f"{self.prompt_style}.j2"
        if not path.exists():
            path = PROMPT_DIR / "balanced.j2"
        tmpl = path.read_text()
        u_turns = [m for m in recent_messages if m.get("role") == "user"][-self.recent_user_turns:]
        a_turns = [m for m in recent_messages if m.get("role") == "assistant"][-self.recent_assistant_turns:]
        joined_user = "\n".join(t.get("content", "") for t in u_turns)
        joined_asst = "\n".join(t.get("content", "") for t in a_turns)
        return (
            tmpl
            .replace("{{ user_turns }}", joined_user)
            .replace("{{ assistant_turns }}", joined_asst)
        )
```

```jinja
{# extensions/active-memory/prompts/balanced.j2 #}
You are a memory-recall sub-agent. Decide if any prior memory is relevant to the user's most recent turn.

User's recent turns:
{{ user_turns }}

Assistant's recent turns:
{{ assistant_turns }}

You may call ``memory_search(query, limit)`` once if helpful. Then return ONLY a JSON object:

  {"action": "inject", "summary": "<one paragraph of relevant context>"}

OR

  {"action": "skip", "summary": ""}

Default: skip. Inject only when context will materially change the assistant's reply. No explanation.
```

- [ ] **Step 3: Run tests → pass**

Run: `pytest tests/active_memory/test_runtime.py -v`
Expected: 3 PASS.

- [ ] **Step 4: Commit**

```bash
git add extensions/active-memory/runtime.py extensions/active-memory/prompts/balanced.j2 tests/active_memory/test_runtime.py
git commit -m "feat(active-memory): bounded sub-agent runner + balanced prompt (1.B step 2)

Wraps provider.complete() with asyncio.wait_for(timeout). Fail-open on
timeout/exception/invalid-JSON. Renders Jinja2-lite prompt with
recent user + assistant turns. Default style 'balanced'."
```

### Task B3: Plugin entrypoint + manifest + hook registration

**Files:**
- Create: `extensions/active-memory/plugin.py`
- Create: `extensions/active-memory/plugin.json`
- Test: `tests/active_memory/test_plugin.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/active_memory/test_plugin.py
"""Tests for active-memory plugin registration."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_plugin_registers_pre_llm_call_hook(monkeypatch, tmp_path):
    """Plugin's register(api) hooks into HookEvent.PRE_LLM_CALL."""
    import sys
    monkeypatch.syspath_prepend("extensions/active-memory")
    from plugin import register

    registered: list = []

    class _StubAPI:
        def register_hook(self, spec): registered.append(spec)
        def slash_commands(self): return SlashRegistry()
        @property
        def memory(self): return None
        @property
        def provider(self): return None

    class SlashRegistry:
        def add(self, *a, **kw): pass

    api = _StubAPI()
    manifest = register(api)
    assert manifest.id == "active-memory"
    assert any(s.event.value == "PreLLMCall" for s in registered)


@pytest.mark.asyncio
async def test_plugin_hook_returns_modified_message_on_inject(monkeypatch, tmp_path):
    """When sub-agent decides 'inject', hook returns HookDecision with
    ``modified_message`` containing the <relevant-memories> block."""
    import sys
    monkeypatch.syspath_prepend("extensions/active-memory")
    from plugin import _build_hook_handler

    async def fake_runner_run(**kw):
        return {"action": "inject", "summary": "User prefers tea over coffee."}

    handler = _build_hook_handler(
        runner_run=fake_runner_run,
        cache=None,  # no caching for this test
        allowed_chat_types=("dm", "group"),
    )

    from plugin_sdk.hooks import HookContext, HookEvent
    ctx = HookContext(
        event=HookEvent.PRE_LLM_CALL,
        session_id="sess-1",
        messages=[{"role": "user", "content": "what do I drink?"}],
        model="claude-haiku-4-5-20251001",
    )
    decision = await handler(ctx)
    assert decision is not None
    assert "<relevant-memories>" in decision.modified_message
    assert "tea over coffee" in decision.modified_message


@pytest.mark.asyncio
async def test_plugin_hook_passes_when_skip():
    import sys
    sys.path.insert(0, "extensions/active-memory")
    from plugin import _build_hook_handler

    async def fake_runner_run(**kw):
        return {"action": "skip", "summary": ""}

    handler = _build_hook_handler(
        runner_run=fake_runner_run,
        cache=None,
        allowed_chat_types=("dm", "group"),
    )

    from plugin_sdk.hooks import HookContext, HookEvent
    ctx = HookContext(
        event=HookEvent.PRE_LLM_CALL,
        session_id="sess-1",
        messages=[{"role": "user", "content": "hello"}],
        model="x",
    )
    decision = await handler(ctx)
    assert decision is None or decision.modified_message == ""
```

- [ ] **Step 2: Implement plugin entrypoint**

```python
# extensions/active-memory/plugin.py
"""Active Memory plugin — proactive pre-reply recall sub-agent.

Hooks into ``HookEvent.PRE_LLM_CALL``. On every eligible main-agent reply,
the bounded sub-agent gets a chance to call memory_search/memory_get and
inject the result as a system reminder via HookDecision.modified_message.

Off by default; enable via ``oc plugin enable active-memory``.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable

from plugin_sdk.core import PluginManifest, PluginKind
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

# Local plugin imports (sys.path is set up by OC plugin loader)
from cache import DecisionCache  # type: ignore[import-not-found]
from runtime import ActiveMemoryRunner  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "enabled": True,
    "model": "gpt-4o-mini",
    "model_fallback": "claude-haiku-4-5-20251001",
    "allowed_chat_types": ["dm", "group"],
    "timeout_ms": 8000,
    "query_mode": "message",
    "prompt_style": "balanced",
    "recent_user_turns": 3,
    "recent_assistant_turns": 1,
    "cache_ttl_ms": 60_000,
    "persist_transcripts": False,
}


def _hash_msg(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _build_hook_handler(
    *,
    runner_run: Callable[..., Any],
    cache: DecisionCache | None,
    allowed_chat_types: tuple[str, ...],
):
    async def handler(ctx: HookContext) -> HookDecision | None:
        # Find last user msg + chat_id
        if not ctx.messages:
            return None
        last_user = next(
            (m for m in reversed(ctx.messages) if m.get("role") == "user"),
            None,
        )
        if last_user is None:
            return None
        content = last_user.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        chat_id = ctx.session_id  # use session_id as chat-key for cache
        msg_hash = _hash_msg(content)

        # Cache check
        if cache is not None:
            cached = cache.get(chat_id, msg_hash)
            if cached is not None:
                if cached["action"] == "inject":
                    return _decision_from_summary(cached["summary"])
                return None

        # Run bounded sub-agent
        decision = await runner_run(chat_id=chat_id, recent_messages=ctx.messages)
        if cache is not None:
            cache.put(chat_id, msg_hash, decision)

        if decision["action"] != "inject":
            return None
        return _decision_from_summary(decision["summary"])

    return handler


def _decision_from_summary(summary: str) -> HookDecision:
    block = (
        "<relevant-memories>\n"
        f"{summary.strip()}\n"
        "</relevant-memories>"
    )
    return HookDecision(decision="pass", modified_message=block)


def register(api) -> PluginManifest:
    cfg: dict = {**DEFAULT_CONFIG, **(getattr(api, "config", {}) or {})}

    if not cfg["enabled"]:
        logger.info("active-memory disabled by config")
        return PluginManifest(
            id="active-memory",
            name="Active Memory",
            kind=PluginKind.MIXED,
            description="Pre-reply blocking memory recall sub-agent",
            version="0.1.0",
        )

    runner = ActiveMemoryRunner(
        provider=api.provider,
        memory=api.memory,
        model=cfg["model"],
        timeout_ms=cfg["timeout_ms"],
        prompt_style=cfg["prompt_style"],
        recent_user_turns=cfg["recent_user_turns"],
        recent_assistant_turns=cfg["recent_assistant_turns"],
    )
    cache = DecisionCache(ttl_ms=cfg["cache_ttl_ms"])

    handler = _build_hook_handler(
        runner_run=runner.run,
        cache=cache,
        allowed_chat_types=tuple(cfg["allowed_chat_types"]),
    )

    api.register_hook(HookSpec(
        event=HookEvent.PRE_LLM_CALL,
        handler=handler,
        priority=200,
    ))

    return PluginManifest(
        id="active-memory",
        name="Active Memory",
        kind=PluginKind.MIXED,
        description="Pre-reply blocking memory recall sub-agent",
        version="0.1.0",
    )
```

```json
{
  "id": "active-memory",
  "name": "Active Memory",
  "kind": "mixed",
  "version": "0.1.0",
  "entry": "plugin.py",
  "description": "Pre-reply blocking memory-recall sub-agent (OpenClaw 1.B port)"
}
```

- [ ] **Step 3: Run tests → pass**

Run: `pytest tests/active_memory/test_plugin.py -v`
Expected: 3 PASS.

- [ ] **Step 4: Commit**

```bash
git add extensions/active-memory/plugin.py extensions/active-memory/plugin.json tests/active_memory/test_plugin.py
git commit -m "feat(active-memory): plugin entrypoint + PreLLMCall hook (1.B step 3)

Plugin's register(api) instantiates ActiveMemoryRunner + DecisionCache
and registers a PreLLMCall hook handler. On 'inject' decision, returns
HookDecision with modified_message='<relevant-memories>...'. On 'skip',
returns None — main reply proceeds unchanged."
```

### Task B4: Honor modified_message in agent loop (only if Phase 0.1 Outcome B)

**This task ONLY runs if Phase 0.1 found that PreLLMCall doesn't currently inject modified_message.**

**Files:**
- Modify: `opencomputer/agent/loop.py`

- [ ] **Step 1: Read agent/loop.py around the PRE_LLM_CALL emit point**

Find the line where `HookEngine.emit(HookEvent.PRE_LLM_CALL, ...)` is called.

- [ ] **Step 2: After emit, scan returned decisions for non-empty modified_message + insert into messages**

```python
# In agent/loop.py — immediately after PreLLMCall emit
decisions = await self.hooks.emit(HookEvent.PRE_LLM_CALL, ctx)
for d in decisions:
    if d and d.modified_message:
        # Inject as a system reminder right before the next provider call
        messages.append({
            "role": "user",  # use "user" role with system-reminder framing for Anthropic API compat
            "content": f"<system-reminder>\n{d.modified_message}\n</system-reminder>"
        })
```

- [ ] **Step 3: Add a regression test**

```python
# tests/test_agent_loop.py — append
@pytest.mark.asyncio
async def test_pre_llm_call_modified_message_injected_into_messages():
    """If a PreLLMCall hook returns modified_message, the next provider call sees it."""
    # ... wire up agent loop with a stub hook returning modified_message="<test>";
    # assert provider.complete was called with messages containing "<test>".
    ...  # full test is engineer-written using existing AgentLoop test fixtures
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_agent_loop.py -v -k modified_message`
Expected: PASS.

- [ ] **Step 5: Commit (only if this task ran)**

```bash
git add opencomputer/agent/loop.py tests/test_agent_loop.py
git commit -m "fix(agent): honor PreLLMCall HookDecision.modified_message

The PreLLMCall hook's HookDecision.modified_message field was previously
not honored (designed for PreToolUse). With Active Memory using it as the
injection channel, the agent loop now appends a <system-reminder>-wrapped
modified_message to the message list before each provider.complete() call.

Backward compatible — hooks that don't set modified_message are unaffected."
```

### Task B5: Slash commands `/active-memory pause|resume|status`

**Files:**
- Create: `extensions/active-memory/slash_commands.py`
- Test: `tests/active_memory/test_slash_commands.py`

- [ ] **Step 1: Write failing test**

```python
# tests/active_memory/test_slash_commands.py
"""Tests for /active-memory slash commands."""
import pytest


@pytest.mark.asyncio
async def test_pause_sets_session_flag():
    import sys; sys.path.insert(0, "extensions/active-memory")
    from slash_commands import handle_pause
    out = await handle_pause(session={}, args="")
    assert out["paused"] is True


@pytest.mark.asyncio
async def test_resume_clears_session_flag():
    import sys; sys.path.insert(0, "extensions/active-memory")
    from slash_commands import handle_resume
    out = await handle_resume(session={"active_memory_paused": True}, args="")
    assert out["paused"] is False


@pytest.mark.asyncio
async def test_status_returns_state():
    import sys; sys.path.insert(0, "extensions/active-memory")
    from slash_commands import handle_status
    out = await handle_status(session={"active_memory_paused": False}, args="")
    assert "paused" in out
```

- [ ] **Step 2: Implement**

```python
# extensions/active-memory/slash_commands.py
"""Slash commands for active-memory plugin."""
from __future__ import annotations

from typing import Any


async def handle_pause(*, session: dict, args: str) -> dict[str, Any]:
    session["active_memory_paused"] = True
    return {"paused": True, "message": "Active Memory paused for this session."}


async def handle_resume(*, session: dict, args: str) -> dict[str, Any]:
    session["active_memory_paused"] = False
    return {"paused": False, "message": "Active Memory resumed."}


async def handle_status(*, session: dict, args: str) -> dict[str, Any]:
    paused = session.get("active_memory_paused", False)
    return {"paused": paused, "message": f"Active Memory {'paused' if paused else 'active'}."}
```

In `plugin.py::register(api)`, add slash registration after the hook spec:

```python
api.slash_commands().add("active-memory pause", handle_pause)
api.slash_commands().add("active-memory resume", handle_resume)
api.slash_commands().add("active-memory status", handle_status)
```

Also add a guard in `_build_hook_handler` to respect `session.active_memory_paused`:

```python
# Inside handler, near top:
if ctx.runtime and getattr(ctx.runtime, "session_state", {}).get("active_memory_paused"):
    return None
```

- [ ] **Step 3: Run tests → pass**

Run: `pytest tests/active_memory/test_slash_commands.py -v`
Expected: 3 PASS.

- [ ] **Step 4: Commit**

```bash
git add extensions/active-memory/slash_commands.py tests/active_memory/test_slash_commands.py extensions/active-memory/plugin.py
git commit -m "feat(active-memory): /active-memory pause|resume|status slash commands"
```

### Task B6: Add 5 alternative prompt styles

**Files:**
- Create: `extensions/active-memory/prompts/strict.j2`
- Create: `extensions/active-memory/prompts/contextual.j2`
- Create: `extensions/active-memory/prompts/recall_heavy.j2`
- Create: `extensions/active-memory/prompts/precision_heavy.j2`
- Create: `extensions/active-memory/prompts/preference_only.j2`

- [ ] **Step 1:** Copy `balanced.j2` to each name; tweak the "default: skip" or "default: inject" + threshold language for each style.

  - `strict.j2`: "Inject ONLY when at least 2 prior memory entries directly bear on the user's question."
  - `contextual.j2`: "Inject context that is relevant to ongoing topics in the recent conversation."
  - `recall_heavy.j2`: "Bias toward inject. When in doubt, inject a 1-line summary of the most recent topical memory."
  - `precision_heavy.j2`: "Bias toward skip. Inject only when memory contains a hard fact (name, date, decision)."
  - `preference_only.j2`: "Inject only memory entries marked as user preferences (likes/dislikes/style)."

- [ ] **Step 2:** Add a parameterized test:

```python
# tests/active_memory/test_runtime.py — append
@pytest.mark.parametrize("style", [
    "balanced", "strict", "contextual", "recall_heavy", "precision_heavy", "preference_only",
])
def test_each_prompt_style_renders(style):
    import sys; sys.path.insert(0, "extensions/active-memory")
    from runtime import ActiveMemoryRunner
    r = ActiveMemoryRunner(provider=None, memory=None, model="x", prompt_style=style)
    rendered = r._render_prompt([{"role": "user", "content": "hi"}])
    assert "User's recent turns:" in rendered or "User turns" in rendered
```

- [ ] **Step 3:** Run + commit + push branch:

```bash
pytest tests/active_memory/test_runtime.py -v -k prompt_style
git add extensions/active-memory/prompts/
git commit -m "feat(active-memory): 5 alternative prompt styles + parametrized rendering test"
git push -u origin feat/openclaw-1b-active-memory
gh pr create --title "feat(active-memory): pre-reply blocking sub-agent (OpenClaw 1.B)" --body "..."
```

---

## Sub-project C — Anti-loop / repetition detector

**Goal:** Detect degenerate tool-loops or text-repetition; warn the agent; abort after persistent flagging.

**Branch:** `feat/openclaw-1c-anti-loop`.

**Files:**
- Create: `opencomputer/agent/loop_safety.py`
- Create: `tests/agent/test_loop_safety.py`
- Modify: `opencomputer/agent/loop.py` — wire detector into the loop iteration

### Task C1: `LoopDetector` class with sliding-window repetition tracking

**Files:**
- Create: `opencomputer/agent/loop_safety.py`
- Test: `tests/agent/test_loop_safety.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_loop_safety.py
"""Tests for the LoopDetector + LoopAbortError."""
from __future__ import annotations

import pytest


def test_detector_flags_third_repeat_tool_call():
    from opencomputer.agent.loop_safety import LoopDetector
    d = LoopDetector(max_tool_repeats=3, max_text_repeats=2, window_size=10)
    for _ in range(2):
        d.record_tool_call("Bash", "h-1")
        assert not d.flagged()
    d.record_tool_call("Bash", "h-1")
    assert d.flagged()
    assert "Bash" in d.warning()
    assert not d.must_stop()


def test_detector_must_stop_after_consecutive_flags():
    from opencomputer.agent.loop_safety import LoopDetector
    d = LoopDetector(max_tool_repeats=2, max_text_repeats=2,
                     window_size=10, max_consecutive_flags=2)
    d.record_tool_call("Bash", "h-1")
    d.record_tool_call("Bash", "h-1")
    assert d.flagged()
    d.record_tool_call("Bash", "h-1")  # second consecutive flag
    assert d.must_stop()


def test_detector_resets_when_unique_tool_call():
    from opencomputer.agent.loop_safety import LoopDetector
    d = LoopDetector(max_tool_repeats=2, max_text_repeats=2)
    d.record_tool_call("Bash", "h-1")
    d.record_tool_call("Bash", "h-1")
    assert d.flagged()
    d.record_tool_call("Read", "h-2")
    assert not d.flagged()


def test_detector_text_repetition():
    from opencomputer.agent.loop_safety import LoopDetector
    d = LoopDetector(max_tool_repeats=10, max_text_repeats=2)
    d.record_assistant_text("h-X")
    d.record_assistant_text("h-X")
    assert d.flagged()


def test_detector_window_bounds_memory():
    from opencomputer.agent.loop_safety import LoopDetector
    d = LoopDetector(max_tool_repeats=3, max_text_repeats=2, window_size=5)
    for i in range(20):
        d.record_tool_call("X", str(i))
    # buffer never exceeds window_size
    assert len(d._tool_window) == 5


def test_loop_abort_error_is_subclass_of_runtime_error():
    from opencomputer.agent.loop_safety import LoopAbortError
    assert issubclass(LoopAbortError, RuntimeError)
```

- [ ] **Step 2: Implement**

```python
# opencomputer/agent/loop_safety.py
"""Anti-loop / repetition detector for the agent loop.

Tracks the last N (tool_name, args_hash) pairs in a sliding window. If the
same pair recurs more than ``max_tool_repeats`` times within the window,
flag a warning. After ``max_consecutive_flags`` consecutive flagged calls,
the detector signals must-stop and the agent loop aborts.

Symmetric for assistant text-hash repetition.

Default thresholds are permissive — healthy sessions never trigger.
"""
from __future__ import annotations

from collections import deque
from typing import Deque


class LoopAbortError(RuntimeError):
    """Raised by the agent loop when LoopDetector.must_stop() returns True."""


class LoopDetector:
    def __init__(
        self,
        *,
        max_tool_repeats: int = 3,
        max_text_repeats: int = 2,
        window_size: int = 10,
        max_consecutive_flags: int = 2,
    ) -> None:
        self.max_tool_repeats = max_tool_repeats
        self.max_text_repeats = max_text_repeats
        self.window_size = window_size
        self.max_consecutive_flags = max_consecutive_flags
        self._tool_window: Deque[tuple[str, str]] = deque(maxlen=window_size)
        self._text_window: Deque[str] = deque(maxlen=window_size)
        self._consecutive_flags = 0
        self._last_warning: str = ""

    def record_tool_call(self, name: str, args_hash: str) -> None:
        key = (name, args_hash)
        self._tool_window.append(key)
        count = sum(1 for k in self._tool_window if k == key)
        if count >= self.max_tool_repeats:
            self._consecutive_flags += 1
            self._last_warning = (
                f"You are repeating tool call {name} with identical args "
                f"({count}× within last {self.window_size}). "
                f"Either change approach or call AskUserQuestion."
            )
        else:
            self._consecutive_flags = 0
            self._last_warning = ""

    def record_assistant_text(self, text_hash: str) -> None:
        self._text_window.append(text_hash)
        count = sum(1 for h in self._text_window if h == text_hash)
        if count >= self.max_text_repeats:
            self._consecutive_flags += 1
            self._last_warning = (
                f"You are repeating the same assistant message ({count}× "
                f"within last {self.window_size}). Try a fresh approach or stop."
            )

    def flagged(self) -> bool:
        return bool(self._last_warning)

    def warning(self) -> str:
        return self._last_warning

    def must_stop(self) -> bool:
        return self._consecutive_flags >= self.max_consecutive_flags

    def summary(self) -> str:
        return self._last_warning or "loop detector aborted"

    def reset(self) -> None:
        self._tool_window.clear()
        self._text_window.clear()
        self._consecutive_flags = 0
        self._last_warning = ""
```

- [ ] **Step 3: Run tests → pass**

Run: `pytest tests/agent/test_loop_safety.py -v`
Expected: 6 PASS.

- [ ] **Step 4: Commit**

```bash
git add opencomputer/agent/loop_safety.py tests/agent/test_loop_safety.py
git commit -m "feat(agent): LoopDetector + LoopAbortError (1.C step 1)

Sliding-window detector for (tool_name, args_hash) and assistant text-hash
repetition. Default thresholds (3 tool repeats, 2 text repeats, window=10,
max-consecutive-flags=2) almost never fire on healthy sessions."
```

### Task C2: Wire detector into agent/loop.py

**Files:**
- Modify: `opencomputer/agent/loop.py`

- [ ] **Step 1: Read AgentLoop's tool dispatch and assistant-text emit points**

Find the section where:
- a tool call is dispatched (`tool.run(args)` or `dispatch_tool_call(...)`)
- an assistant message is added to messages

- [ ] **Step 2: Add detector instantiation + recordings**

Inside `AgentLoop.__init__`:

```python
from opencomputer.agent.loop_safety import LoopAbortError, LoopDetector

self._loop_detector = LoopDetector()
```

After each tool call result:

```python
import hashlib, json

args_hash = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]
self._loop_detector.record_tool_call(tool_call.name, args_hash)
if self._loop_detector.flagged():
    messages.append({"role": "user", "content": f"<system-reminder>{self._loop_detector.warning()}</system-reminder>"})
if self._loop_detector.must_stop():
    raise LoopAbortError(self._loop_detector.summary())
```

After each assistant text emit:

```python
text_hash = hashlib.sha256((assistant_text or "").encode()).hexdigest()[:16]
self._loop_detector.record_assistant_text(text_hash)
if self._loop_detector.must_stop():
    raise LoopAbortError(self._loop_detector.summary())
```

In `run_conversation`'s outer try/except, catch `LoopAbortError`:

```python
except LoopAbortError as e:
    return f"Agent loop stopped: {e}"
```

Reset on session boundary:

```python
# In AgentLoop.run_conversation start:
self._loop_detector.reset()
```

- [ ] **Step 3: Add integration test**

```python
# tests/agent/test_loop_safety.py — append
@pytest.mark.asyncio
async def test_agent_loop_aborts_on_4_identical_bash_calls(monkeypatch):
    """Synthetic: feed AgentLoop a provider that always asks for Bash(ls)."""
    # ... build minimal AgentLoop with a stub provider that returns
    # 4 successive Bash(ls) tool calls; assert outer call returns
    # "Agent loop stopped: ..." string.
    ...  # use existing AgentLoop test fixtures
```

- [ ] **Step 4: Run tests → pass**

Run: `pytest tests/agent/ -v`
Expected: existing tests + new tests all PASS.

- [ ] **Step 5: Commit + push + PR**

```bash
git add opencomputer/agent/loop.py tests/agent/test_loop_safety.py
git commit -m "feat(agent): wire LoopDetector into AgentLoop (1.C step 2)

Records tool calls + assistant text hashes per iteration; appends a
<system-reminder> warning to messages on flag; raises LoopAbortError
on must_stop, caught in run_conversation outer handler and surfaced
as 'Agent loop stopped: <reason>'."
git push -u origin feat/openclaw-1c-anti-loop
gh pr create --title "feat(agent): anti-loop / repetition detector (OpenClaw 1.C)" --body "..."
```

---

## Sub-project D — Replay sanitization

**Goal:** On gateway restart / channel reconnect, drop stale assistant turns + outgoing-queue duplicates + over-aged user messages before re-feeding to dispatch.

**Branch:** `feat/openclaw-1d-replay-sanitization`.

**Files:**
- Create: `opencomputer/gateway/replay_sanitizer.py`
- Create: `tests/gateway/test_replay_sanitizer.py`
- Modify: `opencomputer/gateway/server.py` — call sanitizer in `_replay_pending` (or equivalent)

### Task D1: `sanitize_for_replay` function

**Files:**
- Create: `opencomputer/gateway/replay_sanitizer.py`
- Test: `tests/gateway/test_replay_sanitizer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_replay_sanitizer.py
"""Tests for replay sanitization."""
from __future__ import annotations

import time

import pytest


def _msg(role, content, age_s=0, replay=False, in_flight=False):
    return {
        "role": role,
        "content": content,
        "ts": time.time() - age_s,
        "replay": replay,
        "in_flight": in_flight,
    }


def test_strip_replay_marked_assistant():
    from opencomputer.gateway.replay_sanitizer import sanitize_for_replay
    msgs = [
        _msg("user", "hi"),
        _msg("assistant", "buffered reply", replay=True),
        _msg("user", "next"),
    ]
    out = sanitize_for_replay(msgs, max_age_seconds=300)
    assert all(m["role"] != "assistant" for m in out if m.get("replay"))
    assert len(out) == 2  # user + user


def test_drop_in_flight_outgoing():
    from opencomputer.gateway.replay_sanitizer import sanitize_for_replay
    msgs = [
        _msg("user", "ping"),
        _msg("assistant", "pong", in_flight=True),
        _msg("user", "next"),
    ]
    out = sanitize_for_replay(msgs)
    assert all(not m.get("in_flight") for m in out)


def test_drop_user_messages_older_than_max_age():
    from opencomputer.gateway.replay_sanitizer import sanitize_for_replay
    msgs = [
        _msg("user", "stale", age_s=600),
        _msg("user", "fresh", age_s=10),
    ]
    out = sanitize_for_replay(msgs, max_age_seconds=300)
    assert [m["content"] for m in out] == ["fresh"]


def test_default_passthrough():
    from opencomputer.gateway.replay_sanitizer import sanitize_for_replay
    msgs = [
        _msg("user", "a"),
        _msg("assistant", "b"),
        _msg("user", "c"),
    ]
    out = sanitize_for_replay(msgs)
    assert len(out) == 3
```

- [ ] **Step 2: Implement**

```python
# opencomputer/gateway/replay_sanitizer.py
"""Sanitize a message list before re-feeding into dispatch on cold start.

Drops:
  - Assistant turns marked ``replay=True`` (already streamed; would double-send)
  - Outgoing-queue items still ``in_flight=True`` (will be retried by the queue itself)
  - User messages older than ``max_age_seconds`` (likely stale or already handled)

Preserves order and identity for everything else.
"""
from __future__ import annotations

import time
from typing import Any


def sanitize_for_replay(
    messages: list[dict[str, Any]],
    *,
    max_age_seconds: int = 300,
) -> list[dict[str, Any]]:
    cutoff = time.time() - max_age_seconds
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.get("replay"):
            continue
        if m.get("in_flight"):
            continue
        if m.get("role") == "user" and m.get("ts", time.time()) < cutoff:
            continue
        out.append(m)
    return out
```

- [ ] **Step 3: Run + commit**

```bash
pytest tests/gateway/test_replay_sanitizer.py -v
git add opencomputer/gateway/replay_sanitizer.py tests/gateway/test_replay_sanitizer.py
git commit -m "feat(gateway): sanitize_for_replay — drop stale/in-flight/over-aged on restart"
```

### Task D2: Wire sanitizer into gateway cold start

**Files:**
- Modify: `opencomputer/gateway/server.py`

- [ ] **Step 1:** Find the function that re-feeds buffered messages on Gateway startup or channel reconnect (search for `replay`, `pending`, or `reconnect`). Document its current behavior.

- [ ] **Step 2:** Insert call:

```python
from opencomputer.gateway.replay_sanitizer import sanitize_for_replay

# In _replay_pending or equivalent:
messages = sanitize_for_replay(messages, max_age_seconds=300)
for m in messages:
    await self.dispatch.handle_message(m)
```

- [ ] **Step 3:** Add an integration test that simulates restart with a mix of stale/in-flight/fresh messages → only fresh ones reach dispatch.

- [ ] **Step 4:** Run + commit + push + PR.

```bash
pytest tests/gateway/ -v
git add opencomputer/gateway/server.py
git commit -m "feat(gateway): wire sanitize_for_replay into cold-start replay path"
git push -u origin feat/openclaw-1d-replay-sanitization
gh pr create --title "feat(gateway): replay sanitization (OpenClaw 1.D)" --body "..."
```

---

## Sub-project E — Auth profile rotation cooldown + auto-monitor

**Goal:** Add time-based cooldown to credential pool (currently hard-fail-only) + opt-in periodic doctor-style monitor that proactively pings each profile.

**Branch:** `feat/openclaw-1e-auth-cooldown`.

**Files:**
- Create or modify: `plugin_sdk/credential_pool.py` (find existing location via grep first)
- Create: `opencomputer/doctor.py::auth_monitor_loop()` (extend existing doctor)
- Test: `tests/test_credential_pool_cooldown.py`
- Test: `tests/test_auth_monitor.py`

### Task E1: Add `cooldown(profile_id, seconds)` to CredentialPool

**Files:**
- Modify: existing credential pool module (to be located via Phase 0.5)
- Test: `tests/test_credential_pool_cooldown.py`

- [ ] **Step 1:** Run `grep -rn "class CredentialPool\|class.*Pool.*credential" plugin_sdk/ extensions/ opencomputer/` to locate the pool module. Document path.

- [ ] **Step 2: Write failing test**

```python
# tests/test_credential_pool_cooldown.py
import pytest

@pytest.fixture
def pool():
    # Engineer: import from located path
    from plugin_sdk.credential_pool import CredentialPool  # adjust path
    return CredentialPool(profiles=["A", "B", "C"])


def test_cooldown_demotes_profile(pool):
    pool.cooldown("B", seconds=10)
    available = list(pool.available_profiles())
    assert "B" not in available
    assert "A" in available and "C" in available


def test_cooldown_expires_and_restores(pool, monkeypatch):
    import time
    pool.cooldown("B", seconds=1)
    # advance monotonic
    monkeypatch.setattr(time, "monotonic", lambda: time.monotonic() + 2)
    available = list(pool.available_profiles())
    assert "B" in available
```

- [ ] **Step 3: Implement** (additive — preserves existing pool behavior).

Pseudo-implementation (engineer adapts to located module):

```python
# In CredentialPool
import time

def __init__(self, profiles, ...):
    self._profiles = list(profiles)
    self._cooldown_until: dict[str, float] = {}

def cooldown(self, profile_id: str, seconds: int) -> None:
    self._cooldown_until[profile_id] = time.monotonic() + seconds

def available_profiles(self):
    now = time.monotonic()
    for p in self._profiles:
        until = self._cooldown_until.get(p, 0)
        if until <= now:
            yield p
```

- [ ] **Step 4: Run + commit**

```bash
pytest tests/test_credential_pool_cooldown.py -v
git add tests/test_credential_pool_cooldown.py # plus modified pool path
git commit -m "feat(credential-pool): time-based cooldown demotes failing profiles (1.E step 1)"
```

### Task E2: Provider plugins call `cooldown()` on transient errors

**Files:**
- Modify: `extensions/anthropic-provider/provider.py`, `extensions/openai-provider/provider.py`

- [ ] **Step 1:** In each provider's error-handling block, add for each retry/permission exception type:

```python
if isinstance(e, (httpx.ConnectError, httpx.ReadTimeout)) or (
    hasattr(e, "status_code") and 500 <= e.status_code < 600
):
    self.credential_pool.cooldown(self.current_profile_id, seconds=60)
```

- [ ] **Step 2:** Add a test using stub provider that raises `httpx.ConnectError` and assert the profile gets cooled down.

- [ ] **Step 3:** Run + commit.

### Task E3: `auth_monitor_loop` background task

**Files:**
- Modify: `opencomputer/doctor.py`
- Test: `tests/test_auth_monitor.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_auth_monitor.py
import pytest
import asyncio

@pytest.mark.asyncio
async def test_monitor_demotes_failing_profile_and_restores_recovered(monkeypatch):
    from opencomputer.doctor import auth_monitor_once
    pool_state = {"calls": []}

    class _StubProvider:
        async def ping(self, profile_id):
            pool_state["calls"].append(profile_id)
            if profile_id == "BAD":
                raise RuntimeError("simulated failure")

    class _StubPool:
        def __init__(self): self.cooled = []
        def cooldown(self, p, seconds): self.cooled.append((p, seconds))
        def all_profiles(self): return ["A", "BAD", "C"]

    pool = _StubPool()
    await auth_monitor_once(provider=_StubProvider(), pool=pool, cooldown_seconds=30)
    assert "BAD" in [c[0] for c in pool.cooled]
    assert "A" not in [c[0] for c in pool.cooled]
```

- [ ] **Step 2: Implement**

```python
# opencomputer/doctor.py — append
import asyncio
import logging
logger = logging.getLogger(__name__)


async def auth_monitor_once(*, provider, pool, cooldown_seconds: int = 60) -> None:
    """One pass: ping each profile, demote failing ones."""
    for profile_id in pool.all_profiles():
        try:
            await provider.ping(profile_id)
        except Exception:
            logger.warning("auth_monitor: profile %s failed; cooling down %ds", profile_id, cooldown_seconds)
            pool.cooldown(profile_id, seconds=cooldown_seconds)


async def auth_monitor_loop(*, provider, pool, interval_seconds: int = 300) -> None:
    """Background loop — opt-in via config.yaml::auth.monitor.enabled."""
    while True:
        await auth_monitor_once(provider=provider, pool=pool)
        await asyncio.sleep(interval_seconds)
```

- [ ] **Step 3: Wire opt-in in cli.py / Gateway init**:

```python
# In gateway startup, if config.auth.monitor.enabled:
asyncio.create_task(auth_monitor_loop(...))
```

- [ ] **Step 4: Run + commit + push + PR**

```bash
pytest tests/test_auth_monitor.py tests/test_credential_pool_cooldown.py -v
git push -u origin feat/openclaw-1e-auth-cooldown
gh pr create --title "feat(auth): cooldown + auto-monitor (OpenClaw 1.E)" --body "..."
```

---

## Sub-project F — Sessions-* tools

**Goal:** 5 tools (`SessionsSpawn`, `SessionsSend`, `SessionsList`, `SessionsHistory`, `SessionsStatus`) so the agent can programmatically manage parallel sessions. Behind F1 ConsentGate.

**Branch:** `feat/openclaw-1f-sessions-tools`.

**Files:**
- Create: `opencomputer/tools/sessions.py`
- Create: `tests/tools/test_sessions.py`
- Modify: `opencomputer/cli.py` — register all 5 tools

### Task F1–F5: Five tools, one file, one TDD cycle each

**Pattern (repeat 5 times — one per tool):**

For each of `SessionsSpawn`, `SessionsSend`, `SessionsList`, `SessionsHistory`, `SessionsStatus`:

- [ ] **Step 1: Write failing test**

```python
# tests/tools/test_sessions.py — incremental, append per tool
import pytest


@pytest.mark.asyncio
async def test_sessions_list_returns_session_summaries(monkeypatch, tmp_path):
    from opencomputer.tools.sessions import SessionsList
    # Engineer: build a SessionDB with 2 sessions; instantiate SessionsList(db);
    # call ToolCall("SessionsList", {}); assert result has 2 items with id/created_at/last_active.
    ...
```

- [ ] **Step 2: Implement**

```python
# opencomputer/tools/sessions.py
"""5 tools for programmatic session management.

Behind F1 ConsentGate. Each tool's capability_claims is set so the
ConsentGate prompts on first use. Subsequent calls within a session
are auto-approved per the F1 promotion rules.
"""
from __future__ import annotations

from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class SessionsList(BaseTool):
    capability_claims = ("sessions.list",)

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SessionsList",
            description="List recent sessions for the current profile.",
            input_schema={"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}},
        )

    def __init__(self, db: Any) -> None:
        self.db = db

    async def run(self, call: ToolCall) -> ToolResult:
        limit = int(call.input.get("limit", 20))
        rows = self.db.list_sessions(limit=limit)
        return ToolResult(output=str(rows), is_error=False)


class SessionsSpawn(BaseTool):
    capability_claims = ("sessions.spawn",)

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SessionsSpawn",
            description="Fork a new session from an initial prompt; return the new session_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "prompt": {"type": "string"},
                    "model": {"type": "string", "default": ""},
                },
                "required": ["prompt"],
            },
        )

    def __init__(self, db: Any, runner: Any) -> None:
        self.db = db
        self.runner = runner

    async def run(self, call: ToolCall) -> ToolResult:
        prompt = call.input["prompt"]
        name = call.input.get("name", "")
        model = call.input.get("model") or None
        new_sid = self.db.create_session(name=name)
        await self.runner.spawn_async(new_sid, prompt, model=model)
        return ToolResult(output=new_sid, is_error=False)


class SessionsSend(BaseTool):
    capability_claims = ("sessions.send",)

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SessionsSend",
            description="Enqueue a message into another session.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["session_id", "message"],
            },
        )

    def __init__(self, queue: Any) -> None:
        self.queue = queue

    async def run(self, call: ToolCall) -> ToolResult:
        sid = call.input["session_id"]
        msg = call.input["message"]
        await self.queue.put_session_send(sid, msg)
        return ToolResult(output=f"queued for {sid}", is_error=False)


class SessionsHistory(BaseTool):
    capability_claims = ("sessions.history",)

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SessionsHistory",
            description="Read recent messages from a session.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 30},
                },
                "required": ["session_id"],
            },
        )

    def __init__(self, db: Any) -> None:
        self.db = db

    async def run(self, call: ToolCall) -> ToolResult:
        sid = call.input["session_id"]
        limit = int(call.input.get("limit", 30))
        msgs = self.db.get_messages(sid, limit=limit)
        return ToolResult(output=str(msgs), is_error=False)


class SessionsStatus(BaseTool):
    capability_claims = ("sessions.status",)

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SessionsStatus",
            description="Get is_active / last_message_at / last_tool for a session.",
            input_schema={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
        )

    def __init__(self, db: Any) -> None:
        self.db = db

    async def run(self, call: ToolCall) -> ToolResult:
        sid = call.input["session_id"]
        status = self.db.get_session_summary(sid)
        return ToolResult(output=str(status), is_error=False)
```

- [ ] **Step 3:** Register all 5 in `opencomputer/cli.py` next to `RecallTool`:

```python
from opencomputer.tools.sessions import (
    SessionsList, SessionsSpawn, SessionsSend, SessionsHistory, SessionsStatus,
)

registry.register(SessionsList(db))
registry.register(SessionsSpawn(db, runner=agent_runner))
registry.register(SessionsSend(queue=outgoing_queue))
registry.register(SessionsHistory(db))
registry.register(SessionsStatus(db))
```

- [ ] **Step 4:** Run + commit + push + PR.

```bash
pytest tests/tools/test_sessions.py -v
git add opencomputer/tools/sessions.py tests/tools/test_sessions.py opencomputer/cli.py
git commit -m "feat(tools): SessionsSpawn/Send/List/History/Status (OpenClaw 1.F)

5 tools behind F1 ConsentGate. Each tool's capability_claims set so the
ConsentGate prompts on first use; subsequent calls within a session are
auto-approved per F1 promotion rules."
git push -u origin feat/openclaw-1f-sessions-tools
gh pr create --title "feat(tools): sessions-* (OpenClaw 1.F)" --body "..."
```

---

## Sub-project G — Clarify_tool

**Goal:** A tool the agent can call when the user's prompt is genuinely ambiguous; offers 2-4 concrete options; reuses `AskUserQuestion` machinery.

**Branch:** `feat/openclaw-1g-clarify-tool`.

**Files:**
- Create: `opencomputer/tools/clarify.py`
- Create: `tests/tools/test_clarify.py`
- Modify: `opencomputer/cli.py` — `registry.register(ClarifyTool(...))`

### Task G1: Implement ClarifyTool

- [ ] **Step 1: Write failing test**

```python
# tests/tools/test_clarify.py
import pytest


@pytest.mark.asyncio
async def test_clarify_returns_selected_option(monkeypatch):
    from opencomputer.tools.clarify import ClarifyTool

    class _StubAUQ:
        async def ask(self, *, question, options):
            return options[1]  # always pick option 2

    tool = ClarifyTool(auq=_StubAUQ())
    from plugin_sdk.core import ToolCall
    call = ToolCall(name="Clarify", input={
        "ambiguity": "did you mean A or B?",
        "options": ["A", "B"],
    })
    res = await tool.run(call)
    assert res.output == "B"
```

- [ ] **Step 2: Implement**

```python
# opencomputer/tools/clarify.py
"""ClarifyTool — auto-trigger ambiguity resolution.

Reuses existing AskUserQuestion machinery. Surfaces 2-4 concrete options
to the user (terminal: numbered list; channels: inline buttons via the
adapter's send_approval_request path).
"""
from __future__ import annotations

from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class ClarifyTool(BaseTool):
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Clarify",
            description=(
                "When the user's request is genuinely ambiguous (multiple "
                "plausible interpretations), call this with a list of concrete "
                "options. Do NOT call when the answer is obvious."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ambiguity": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 4,
                    },
                },
                "required": ["ambiguity", "options"],
            },
        )

    def __init__(self, auq: Any) -> None:
        self.auq = auq

    async def run(self, call: ToolCall) -> ToolResult:
        question = call.input["ambiguity"]
        options = list(call.input["options"])
        chosen = await self.auq.ask(question=question, options=options)
        return ToolResult(output=str(chosen), is_error=False)
```

- [ ] **Step 3:** Register in `cli.py`:

```python
from opencomputer.tools.clarify import ClarifyTool
registry.register(ClarifyTool(auq=ask_user_question_handler))
```

- [ ] **Step 4:** Run + commit + push + PR.

```bash
pytest tests/tools/test_clarify.py -v
git push -u origin feat/openclaw-1g-clarify-tool
gh pr create --title "feat(tools): Clarify (OpenClaw 1.G)" --body "..."
```

---

## Sub-project H — Send_message_tool

**Goal:** An agent-callable tool that sends a message to a specific channel + peer via existing `outgoing_queue`. Behind F1 ConsentGate per channel.

**Branch:** `feat/openclaw-1h-send-message-tool`.

**Files:**
- Create: `opencomputer/tools/send_message.py`
- Create: `tests/tools/test_send_message.py`
- Modify: `opencomputer/cli.py` — register

### Task H1: Implement SendMessageTool

- [ ] **Step 1: Failing test**

```python
# tests/tools/test_send_message.py
import pytest


@pytest.mark.asyncio
async def test_send_message_routes_to_outgoing_queue():
    from opencomputer.tools.send_message import SendMessageTool

    sent: list = []

    class _StubQueue:
        async def put_send(self, channel, peer, message):
            sent.append((channel, peer, message))

    tool = SendMessageTool(queue=_StubQueue(), enabled_channels=("telegram", "slack"))
    from plugin_sdk.core import ToolCall
    res = await tool.run(ToolCall(name="SendMessage", input={
        "channel": "telegram",
        "peer": "@dad",
        "message": "review's done",
    }))
    assert res.is_error is False
    assert sent == [("telegram", "@dad", "review's done")]


@pytest.mark.asyncio
async def test_send_message_rejects_disabled_channel():
    from opencomputer.tools.send_message import SendMessageTool

    class _StubQueue:
        async def put_send(self, *a, **kw): raise AssertionError("should not be called")

    tool = SendMessageTool(queue=_StubQueue(), enabled_channels=("slack",))
    from plugin_sdk.core import ToolCall
    res = await tool.run(ToolCall(name="SendMessage", input={
        "channel": "telegram",
        "peer": "@dad",
        "message": "x",
    }))
    assert res.is_error is True
    assert "not enabled" in res.output.lower()
```

- [ ] **Step 2: Implement**

```python
# opencomputer/tools/send_message.py
"""SendMessageTool — agent-callable cross-platform send.

Routes through PluginAPI.outgoing_queue.put_send(channel, peer, message).
Each channel use is gated by F1 ConsentGate (capability_claims:
``messaging.send.<channel>``).
"""
from __future__ import annotations

from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class SendMessageTool(BaseTool):
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SendMessage",
            description=(
                "Send a message to a specific channel and peer. Use only when you "
                "need to deliver content to a destination DIFFERENT from the "
                "current conversation channel."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "enum": list(self._enabled or [])},
                    "peer": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["channel", "peer", "message"],
            },
        )

    def __init__(self, *, queue: Any, enabled_channels: tuple[str, ...]) -> None:
        self.queue = queue
        self._enabled = enabled_channels

    @property
    def capability_claims(self) -> tuple[str, ...]:
        return tuple(f"messaging.send.{c}" for c in self._enabled)

    async def run(self, call: ToolCall) -> ToolResult:
        channel = call.input["channel"]
        peer = call.input["peer"]
        message = call.input["message"]
        if channel not in self._enabled:
            return ToolResult(
                output=f"channel '{channel}' is not enabled.",
                is_error=True,
            )
        await self.queue.put_send(channel, peer, message)
        return ToolResult(output=f"queued to {channel}:{peer}", is_error=False)
```

- [ ] **Step 3:** Register in `cli.py`:

```python
from opencomputer.tools.send_message import SendMessageTool

enabled = tuple(api.list_enabled_channels())
registry.register(SendMessageTool(queue=outgoing_queue, enabled_channels=enabled))
```

- [ ] **Step 4:** Run + commit + push + PR.

```bash
pytest tests/tools/test_send_message.py -v
git push -u origin feat/openclaw-1h-send-message-tool
gh pr create --title "feat(tools): SendMessage (OpenClaw 1.H)" --body "..."
```

---

## Final integration + acceptance gate

After all 8 PRs are open / merged:

- [ ] **Step 1:** Run the full test suite on `main` after each merge:

```bash
pytest tests/ -v --tb=short
```

Expected: zero new failures.

- [ ] **Step 2:** Run ruff + mypy clean:

```bash
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
mypy plugin_sdk/ opencomputer/
```

- [ ] **Step 3:** Verify SDK boundary preserved:

```bash
pytest tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer -v
```

- [ ] **Step 4:** Manual smoke tests (per spec §10 Acceptance):
  - 1.A: Telegram + chunker enabled → multi-paragraph reply delivered paragraph-by-paragraph with 1-2s pauses.
  - 1.B: `oc plugin enable active-memory` → ask "what do I drink?" → agent's reply references prior memory about tea.
  - 1.C: Synthetic 4-identical-Bash-call test → first 3 produce a system-reminder warning, 4th aborts with "Agent loop stopped: ...".
  - 1.D: Restart Gateway with stale messages → only fresh user messages reach dispatch.
  - 1.E: Set bad `ANTHROPIC_API_KEY=bad`, good `ANTHROPIC_API_KEY=good` in pool → bad gets cooled down on transient error → good is used.
  - 1.F: From the agent loop, call `SessionsList()` → returns rows; `SessionsSpawn(prompt="...")` → new session_id.
  - 1.G: Ambiguous prompt "What about it?" → agent calls Clarify with 2 options → user picks → agent proceeds with chosen interpretation.
  - 1.H: Agent calls `SendMessage(channel="telegram", peer="@dad", message="...")` → message appears in Telegram.

- [ ] **Step 5:** Tag the rollup release in CHANGELOG.md once all 8 are on `main`:

```markdown
## [Unreleased] — OpenClaw Tier 1 Port (8 picks)

- 1.A: Block streaming chunker + humanDelay (PR #...)
- 1.B: Active Memory pre-reply sub-agent (PR #...)
- 1.C: Anti-loop / repetition detector (PR #...)
- 1.D: Replay sanitization (PR #...)
- 1.E: Auth profile rotation cooldown + auto-monitor (PR #...)
- 1.F: Sessions-* tools (PR #...)
- 1.G: Clarify tool (PR #...)
- 1.H: SendMessage tool (PR #...)
```

---

## Self-review checklist (run after writing this plan)

**1. Spec coverage:**
- [x] 1.A block chunker → Sub-project A (5 tasks).
- [x] 1.B Active Memory → Sub-project B (6 tasks).
- [x] 1.C anti-loop → Sub-project C (2 tasks).
- [x] 1.D replay sanitization → Sub-project D (2 tasks).
- [x] 1.E auth cooldown + monitor → Sub-project E (3 tasks).
- [x] 1.F sessions-* → Sub-project F (5 tools, single file).
- [x] 1.G Clarify → Sub-project G (1 task).
- [x] 1.H SendMessage → Sub-project H (1 task).
- [x] Phase 0 pre-flight (6 verification tasks).
- [x] Final acceptance gate.

**2. Placeholder scan:** searched for "TBD", "TODO" — only places they appear are in legitimate context (Phase 0.5 "TBD when started" for not-yet-started Hermes tail items in spec; not blocking).

**3. Type consistency:** `BlockChunker`, `Block`, `LoopDetector`, `LoopAbortError`, `DecisionCache`, `ActiveMemoryRunner`, `SessionsList/Spawn/Send/History/Status`, `ClarifyTool`, `SendMessageTool` — all names used identically across tasks. `BaseChannelAdapter._maybe_chunk_delta` and `_flush_chunker` consistent. `HookEvent.PRE_LLM_CALL` consistent. `HookDecision.modified_message` consistent.
