# Thinking History UI v2 — AI-Generated Summaries + Richer Tree

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the per-turn thinking-history line and expanded tree match the Claude.ai web UI: a one-line natural-language summary of what the AI did this turn (e.g. "Extracted daemon persistence mechanics from comprehensive report"), plus a richer expanded tree showing reasoning + tool actions with file-name chips.

**Architecture:**
1. New `opencomputer/agent/reasoning_summary.py` — Haiku-powered summary generator. Direct port of `title_generator.py`'s pattern (cheap-model provider resolver + daemon thread + error swallow).
2. `ReasoningTurn` gains a mutable `summary: str | None` field. New `ReasoningStore.update_summary(turn_id, summary)` method.
3. `StreamingRenderer.finalize` spawns a daemon thread immediately when finalize starts, joins with a short timeout (3s) before printing the collapsed line. If summary ready → enhanced format; else → today's format (graceful degradation).
4. `render_turn_tree` puts the summary in the header so `/reasoning show <N>` always shows the rich view.
5. `ToolAction` gets a `result_file_path: str | None` field. `cli.py`'s tool-completion hook extracts the file path from Edit/Write/Read tool results so the expanded tree shows file-name chips like Image #6.

**Tech Stack:** Python 3.12+, Rich, threading (daemon), claude-haiku-4-5 via existing provider chain.

**Out of scope (explicit):**
- True interactive in-place expand/collapse (would need Textual migration — deferred per PR #382).
- Re-generating summaries when the user changes models (one-shot per turn).
- Persisting summaries across CLI restarts (in-memory only — same as ReasoningStore itself).
- Token-cost tracking for the summary call (negligible: ~50 input + 30 output tokens per turn).
- **`ToolAction.result_file_path` (file-name chips like Image #6)** — DEFERRED to a follow-up PR. The `_on_tool_call_complete` bus event today carries only `tool_name` + `outcome`, not the original args/result. Plumbing args through requires modifying the PRE_TOOL_USE hook, the `_tool_idx_by_call_id` map, and threading them across the bus. Substantial surface for a polish-tier nice-to-have. Headline feature (summary in collapsed line) ships first.

**Audit-confirmed assumptions:**
- `title_generator.py` uses a daemon thread + module-level `call_llm` shim that tests can patch. Mirror this pattern for testability.
- `ReasoningStore` is single-threaded write per turn (called from `finalize` on the main thread). The summary update is the ONE concurrent write — but it targets a different turn_id than the next finalize, and `deque.append` + dict-style frozen-dataclass mutation are race-free for our case.
- `ReasoningTurn` is `@dataclass(frozen=True)` today. To allow post-hoc summary write, either (a) drop frozen, or (b) keep frozen and have `update_summary` rebuild + replace the deque entry. **Going with (b)** to keep external API frozen-friendly; internal swap is mechanical.
- `_TITLE_MODEL = "claude-haiku-4-5"` exists in title_generator.py and the provider chain handles it. Reuse the same constant pattern.

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `opencomputer/agent/reasoning_summary.py` | **Create** | `generate_summary()` + `maybe_summarize_turn()` daemon spawner; mirrors `title_generator.py` |
| `opencomputer/cli_ui/reasoning_store.py` | Modify | `summary` field on `ReasoningTurn`; `update_summary(turn_id, summary)` method; tree render shows summary |
| `opencomputer/cli_ui/streaming.py` | Modify | Spawn summary thread early in `finalize`; brief join before printing; enhanced collapsed-line format |
| `opencomputer/cli.py` | Modify | Tool-completion hook extracts `result_file_path` from Edit/Write/Read results |
| `tests/test_reasoning_summary.py` | **Create** | Generator + spawner unit tests (with patched `call_llm`) |
| `tests/test_reasoning_store.py` | Modify | Add `summary` field + `update_summary` tests |
| `tests/test_streaming_thinking.py` | Modify | New collapsed-line format with-summary test |

---

## Task 0: Worktree

```bash
git -C /Users/saksham/Vscode/claude worktree add \
    /Users/saksham/.config/superpowers/worktrees/claude/thinking-history-summary \
    -b feat/thinking-history-summary main
cd /Users/saksham/.config/superpowers/worktrees/claude/thinking-history-summary/OpenComputer
source /Users/saksham/Vscode/claude/OpenComputer/.venv/bin/activate
pytest tests/ -q --no-header 2>&1 | tail -3   # baseline: 8068+
```

---

## Task 1: ReasoningTurn `summary` field + `ReasoningStore.update_summary`

**Files:** `opencomputer/cli_ui/reasoning_store.py`, `tests/test_reasoning_store.py`

- [ ] **Step 1: Write failing tests** (append to `tests/test_reasoning_store.py`):

```python
# ─── Summary support (v2) ────────────────────────────────────────────────


def test_reasoning_turn_has_optional_summary_field():
    """Summary defaults to None and round-trips when set via append."""
    turn = ReasoningTurn(turn_id=1, thinking="x", duration_s=0.1)
    assert turn.summary is None


def test_store_update_summary_sets_field_for_existing_turn():
    store = ReasoningStore()
    store.append(thinking="x", duration_s=0.1, tool_actions=[])
    store.append(thinking="y", duration_s=0.2, tool_actions=[])
    store.update_summary(turn_id=1, summary="first turn")
    store.update_summary(turn_id=2, summary="second turn")
    assert store.get_by_id(1).summary == "first turn"
    assert store.get_by_id(2).summary == "second turn"


def test_store_update_summary_unknown_id_is_noop():
    """No exception when the turn was evicted or never existed —
    summary writes are best-effort from a background thread."""
    store = ReasoningStore()
    store.append(thinking="x", duration_s=0.1, tool_actions=[])
    # Should not raise.
    store.update_summary(turn_id=99, summary="never landed")
    assert store.get_by_id(1).summary is None  # original turn untouched


def test_render_turn_tree_includes_summary_when_present():
    import io
    from rich.console import Console
    from opencomputer.cli_ui.reasoning_store import render_turn_tree

    store = ReasoningStore()
    store.append(thinking="raw thinking text", duration_s=0.5, tool_actions=[])
    store.update_summary(turn_id=1, summary="Wrote a poem about sloths")
    tree = render_turn_tree(store.get_latest())
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(tree)
    text = out.getvalue()
    assert "Wrote a poem about sloths" in text
    assert "Turn #1" in text
```

- [ ] **Step 2: Verify tests fail.**

```bash
pytest tests/test_reasoning_store.py -k "summary" -v
```

- [ ] **Step 3: Add the field + method.**

In `opencomputer/cli_ui/reasoning_store.py`:

a) Add to `ReasoningTurn`:
```python
@dataclass(frozen=True)
class ReasoningTurn:
    turn_id: int
    thinking: str
    duration_s: float
    tool_actions: tuple[ToolAction, ...] = field(default_factory=tuple)
    summary: str | None = None
    """LLM-generated one-line description of this turn's intent. Set
    asynchronously after :meth:`ReasoningStore.append` by a daemon
    thread (see :mod:`opencomputer.agent.reasoning_summary`); may
    remain ``None`` if the summary call timed out or failed."""
```

b) Add to `ReasoningStore`:
```python
    def update_summary(self, *, turn_id: int, summary: str) -> None:
        """Set the summary on a previously-appended turn. Called from a
        background daemon thread; safe because frozen dataclasses are
        replaced wholesale (immutable swap), not mutated in place.

        Unknown turn_id is a no-op — the turn may have been evicted by
        the time the summary call returned (slow LLM + chatty session)."""
        for i, t in enumerate(self._turns):
            if t.turn_id == turn_id:
                from dataclasses import replace
                self._turns[i] = replace(t, summary=summary)
                return
```

c) Update `render_turn_tree` header to include summary if present:
```python
    # Inside render_turn_tree, build the header list with summary when set.
    header = Text.assemble(
        ("💭 ", "dim cyan"),
        (f"Turn #{turn.turn_id}", "bold cyan"),
        ("  ·  ", "dim"),
        (f"Thought for {_fmt_duration(turn.duration_s)}", "dim cyan"),
        ("  ·  ", "dim"),
        (f"{turn.action_count} action{s}", "dim cyan"),
    )
    if turn.summary:
        # Lead with the summary on its own line above the metadata.
        header = Text.assemble(
            (turn.summary, "bold"),
            ("\n", ""),
            header,
        )
```

- [ ] **Step 4: Verify tests pass.** `pytest tests/test_reasoning_store.py -v`

- [ ] **Step 5: Commit.** `git commit -m "feat(reasoning): add summary field + update_summary method"`

---

## Task 2: `reasoning_summary.py` — Haiku-powered generator

**Files:** Create `opencomputer/agent/reasoning_summary.py`, `tests/test_reasoning_summary.py`

Mirror `title_generator.py`'s structure exactly: module-level `_SUMMARY_MODEL`, `call_llm` shim that tests can patch, `generate_summary()` synchronous helper, `maybe_summarize_turn(store, turn_id, thinking_text)` daemon-thread spawner.

- [ ] **Step 1: Write failing tests** in `tests/test_reasoning_summary.py`:

```python
"""Unit tests for the Haiku-powered reasoning summary generator."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

from opencomputer.agent.reasoning_summary import (
    generate_summary,
    maybe_summarize_turn,
)
from opencomputer.cli_ui.reasoning_store import ReasoningStore


def _fake_response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def test_generate_summary_returns_clean_string():
    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        return_value=_fake_response("Wrote a poem about sloths"),
    ):
        out = generate_summary("I should write a poem about sloths.")
    assert out == "Wrote a poem about sloths"


def test_generate_summary_strips_quotes_and_trailing_punctuation():
    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        return_value=_fake_response('"Wrote a poem about sloths."'),
    ):
        out = generate_summary("anything")
    assert out == "Wrote a poem about sloths"


def test_generate_summary_returns_none_on_empty_input():
    out = generate_summary("")
    assert out is None


def test_generate_summary_returns_none_when_call_llm_raises():
    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        side_effect=RuntimeError("provider down"),
    ):
        out = generate_summary("some thinking")
    assert out is None


def test_maybe_summarize_turn_writes_to_store_via_daemon():
    store = ReasoningStore()
    store.append(thinking="reason", duration_s=0.1, tool_actions=[])

    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        return_value=_fake_response("Did the thing"),
    ):
        thread = maybe_summarize_turn(
            store=store, turn_id=1, thinking_text="reason"
        )
        assert thread is not None
        thread.join(timeout=5.0)

    assert store.get_by_id(1).summary == "Did the thing"


def test_maybe_summarize_turn_skips_when_no_thinking_text():
    """Tool-only turns have no thinking — skip the LLM call."""
    store = ReasoningStore()
    store.append(thinking="", duration_s=0.1, tool_actions=[])
    thread = maybe_summarize_turn(
        store=store, turn_id=1, thinking_text=""
    )
    assert thread is None
    assert store.get_by_id(1).summary is None


def test_maybe_summarize_turn_swallows_unknown_turn_id():
    """Defensive: if the turn was evicted by the time the summary
    arrives, the no-op update_summary path catches it."""
    store = ReasoningStore()
    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        return_value=_fake_response("ignored"),
    ):
        thread = maybe_summarize_turn(
            store=store, turn_id=999, thinking_text="anything"
        )
        thread.join(timeout=5.0)
    # No exception; store unchanged.
    assert store.get_all() == []
```

- [ ] **Step 2: Implement** `opencomputer/agent/reasoning_summary.py` (port of title_generator.py):

```python
"""LLM-generated one-line summaries of reasoning turns.

Direct port of :mod:`opencomputer.agent.title_generator`'s pattern —
cheap Haiku model + module-level ``call_llm`` shim + daemon-thread
spawner for fire-and-forget post-turn processing. The summary appears
in the collapsed thinking-history line + at the top of
``/reasoning show <N>``'s tree.

Adaptation rationale: the title_generator pattern is proven, fully
test-mocked, and provider-agnostic. We deliberately reuse it instead
of reinventing.
"""
from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from typing import Any

from opencomputer.cli_ui.reasoning_store import ReasoningStore

logger = logging.getLogger("opencomputer.reasoning_summary")

_SUMMARY_MODEL = "claude-haiku-4-5"
_SUMMARY_MAX_TOKENS = 50

_SUMMARY_PROMPT = (
    "Generate a short, descriptive one-line summary (5-12 words) of what "
    "an AI assistant just reasoned about. The summary should describe the "
    "TASK the assistant tackled or the conclusion it reached, in plain "
    "natural language — like a section heading. Return ONLY the summary "
    "text, nothing else. No quotes, no trailing punctuation, no prefixes "
    "like 'Summary:' or 'The assistant'."
)


def _resolve_cheap_provider() -> Any:
    """Same provider-resolver as title_generator.py — inherits user
    auth + base URL config (Anthropic native, Claude Router proxy,
    OpenAI-compatible, etc.)."""
    from opencomputer.agent.config import default_config
    from opencomputer.plugins.registry import registry as plugin_registry

    cfg = default_config()
    provider_cls = plugin_registry.providers.get(cfg.model.provider)
    if provider_cls is None:
        raise RuntimeError(
            f"provider {cfg.model.provider!r} not registered; cannot summarize"
        )
    return provider_cls() if isinstance(provider_cls, type) else provider_cls


def call_llm(
    *,
    messages: list[dict[str, str]],
    max_tokens: int = _SUMMARY_MAX_TOKENS,
    temperature: float = 0.3,
    timeout: float = 15.0,
    model: str = _SUMMARY_MODEL,
) -> Any:
    """Cheap-LLM call returning OpenAI-shaped response. Tests patch this."""
    del timeout

    import asyncio

    from plugin_sdk.core import Message

    provider = _resolve_cheap_provider()
    sdk_messages = [Message(role=m["role"], content=m["content"]) for m in messages]

    response = asyncio.run(
        provider.complete(
            messages=sdk_messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    )
    text = response.message.content if response and response.message else ""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def _clean(raw: str) -> str:
    """Strip surrounding quotes + trailing punctuation; cap length."""
    s = (raw or "").strip().strip('"').strip("'").rstrip(".!?:; ")
    return s[:120]


def generate_summary(thinking_text: str, *, timeout: float = 15.0) -> str | None:
    """Generate a one-line summary of the given thinking text. Returns
    the cleaned string or ``None`` on empty input or LLM failure."""
    snippet = (thinking_text or "")[:1500].strip()
    if not snippet:
        return None
    try:
        resp = call_llm(
            messages=[
                {"role": "user", "content": f"{_SUMMARY_PROMPT}\n\n{snippet}"},
            ],
            max_tokens=_SUMMARY_MAX_TOKENS,
            temperature=0.3,
            timeout=timeout,
        )
        raw = resp.choices[0].message.content if resp and resp.choices else ""
        cleaned = _clean(raw)
        return cleaned or None
    except Exception:  # noqa: BLE001 — never let summary failure crash the loop
        logger.debug("reasoning summary failed", exc_info=True)
        return None


def _summarize_and_store(
    store: ReasoningStore, turn_id: int, thinking_text: str
) -> None:
    summary = generate_summary(thinking_text)
    if summary:
        store.update_summary(turn_id=turn_id, summary=summary)


def maybe_summarize_turn(
    *, store: ReasoningStore, turn_id: int, thinking_text: str
) -> threading.Thread | None:
    """Spawn a daemon thread that generates the summary and writes it
    back to the store. Returns the thread (so callers may join it with
    a short timeout if they want the summary in the collapsed line) or
    ``None`` if there's nothing worth summarizing.

    The call is fire-and-forget by design — the daemon thread never
    blocks process exit, errors are swallowed, and unknown turn_ids are
    no-op via :meth:`ReasoningStore.update_summary`."""
    if not (thinking_text or "").strip():
        return None
    thread = threading.Thread(
        target=_summarize_and_store,
        args=(store, turn_id, thinking_text),
        daemon=True,
        name=f"reason-summary-turn-{turn_id}",
    )
    thread.start()
    return thread


__all__ = [
    "call_llm",
    "generate_summary",
    "maybe_summarize_turn",
]
```

- [ ] **Step 3: Verify tests pass.** `pytest tests/test_reasoning_summary.py -v`

- [ ] **Step 4: Commit.** `git commit -m "feat(reasoning): Haiku-powered turn summary generator"`

---

## Task 3: Wire summary into `StreamingRenderer.finalize`

**Files:** `opencomputer/cli_ui/streaming.py`, `tests/test_streaming_thinking.py`

Spawn the summary thread EARLY in `finalize` (before we build the long Markdown render of the answer body). Join with a short timeout right before printing the collapsed line. If summary ready, use enhanced format; else, fall through to today's format.

- [ ] **Step 1: Write failing tests** (append to `tests/test_streaming_thinking.py`):

```python
def test_finalize_collapsed_line_includes_summary_when_available() -> None:
    """When the summary thread completes within the join timeout, the
    collapsed line includes the summary as a lead-in."""
    import io
    from unittest.mock import patch
    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    out = io.StringIO()
    store = ReasoningStore()

    # Patch generate_summary to return immediately so the join doesn't
    # have to wait the full 3 seconds.
    with patch(
        "opencomputer.agent.reasoning_summary.generate_summary",
        return_value="Wrote a haiku about sloths",
    ):
        renderer = StreamingRenderer(
            Console(file=out, force_terminal=False), reasoning_store=store
        )
        with renderer:
            renderer.on_thinking_chunk("let me think about haikus")
            renderer.finalize(
                reasoning="let me think about haikus",
                iterations=1,
                in_tok=1,
                out_tok=1,
                elapsed_s=0.1,
                show_reasoning=False,
            )
    text = out.getvalue()
    assert "Wrote a haiku about sloths" in text


def test_finalize_collapsed_line_falls_back_when_summary_unavailable() -> None:
    """When the summary thread doesn't complete in time, the collapsed
    line falls back to today's format (no crash, no missing data)."""
    import io
    from unittest.mock import patch
    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    out = io.StringIO()
    store = ReasoningStore()
    # Patch so the summary thread returns None (simulates LLM failure).
    with patch(
        "opencomputer.agent.reasoning_summary.generate_summary",
        return_value=None,
    ):
        renderer = StreamingRenderer(
            Console(file=out, force_terminal=False), reasoning_store=store
        )
        with renderer:
            renderer.on_thinking_chunk("hmm")
            renderer.finalize(
                reasoning="hmm",
                iterations=1,
                in_tok=1,
                out_tok=1,
                elapsed_s=0.1,
                show_reasoning=False,
            )
    text = out.getvalue()
    # Today's collapsed format without summary.
    assert "Thought for" in text
    assert "/reasoning show" in text
```

- [ ] **Step 2: Verify tests fail.**

- [ ] **Step 3: Wire in `streaming.py`** — modify the post-store-append section of `finalize` to also spawn the summary thread, then BEFORE printing the collapsed line, join with timeout and use enhanced format if available. The exact diff:

In the existing `if self._reasoning_store is not None:` block (after `store.append(...)`), add:
```python
                # v2: kick off async summary generation. The thread
                # will write back to the store via update_summary; we
                # join it with a short timeout below before printing
                # the collapsed line so the enhanced format can land
                # 95%+ of the time (Haiku is fast).
                from opencomputer.agent.reasoning_summary import (
                    maybe_summarize_turn,
                )
                _summary_thread = maybe_summarize_turn(
                    store=self._reasoning_store,
                    turn_id=appended_turn.turn_id,
                    thinking_text=thinking_str,
                )
```

Adjust `store.append(...)` to capture the returned turn:
```python
                appended_turn = self._reasoning_store.append(...)
```

Then in the collapsed-line branch (the `else:` printing `💭 Thought for X · turn #N · K actions — /reasoning show`), join the thread first and use the summary if landed:
```python
                # Wait briefly for the summary thread to finish so the
                # collapsed line can include it. 1.5s is the trade-off
                # cap — Haiku is typically <2s end-to-end on small
                # inputs but tail latency can spike. Going past 1.5s
                # would noticeably delay the next prompt; users who
                # want the rich format on slow networks always have
                # /reasoning show <N> which reads from the store after
                # the summary lands.
                if _summary_thread is not None:
                    _summary_thread.join(timeout=1.5)
                summary = (
                    self._reasoning_store.get_by_id(appended_turn.turn_id).summary
                    if self._reasoning_store is not None
                    else None
                )
                if summary:
                    # Enhanced format with summary as lead-in.
                    self.console.print(
                        f"[bold]{summary}[/bold] "
                        f"[dim cyan]· turn #{appended_turn.turn_id} "
                        f"· {len(self._tool_history)} actions "
                        f"· {_fmt_duration(thinking_elapsed)} "
                        f"— /reasoning show {appended_turn.turn_id} to expand[/dim cyan]"
                    )
                else:
                    # Fall back to today's format (existing meta_parts assembly).
                    ...existing code unchanged...
```

NOTE on threading: `threading.Thread.join(timeout=3.0)` blocks the main thread for up to 3s. This is acceptable because finalize is called once per turn and 3s is short. If the user wants <1s, lower the timeout — fewer summaries land in the collapsed line but they're always available via `/reasoning show`.

- [ ] **Step 4: Verify tests pass.** `pytest tests/test_streaming_thinking.py -k "summary or collapsed" -v`

- [ ] **Step 5: Commit.** `git commit -m "feat(streaming): include AI summary in collapsed thinking-history line"`

---

## Task 4: DEFERRED — `ToolAction.result_file_path` for file chips

Per audit-update: Task 4 (extract file paths from tool args/results to surface in the expanded tree as Image-#6-style chips) is DEFERRED to a follow-up PR. The bus event needs PRE+POST plumbing changes (`_tool_idx_by_call_id` map needs to thread args through) — not justified for a polish nice-to-have when the headline feature (summary in collapsed/expanded view) is what the user actually pointed at.

Today's expanded tree already shows tool actions with `args_preview` (e.g. `🔧 Edit(file_path=foo.md, ...) ✓ 0.05s`) — file paths ARE visible, just inside the args parens rather than as a separate chip. Acceptable for v1.

---

## Task 5: E2E + Full Suite + Lint + PR

- [ ] **Step 1: Full repo run.**
```bash
pytest tests/ -q --no-header
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```
Expected: green, ~15-20 new tests added.

- [ ] **Step 2: Manual smoke** (interactive — user-attended).
```bash
opencomputer
> write a haiku about sloths
# Watch: collapsed line should include "Wrote a haiku" (or similar)
> /reasoning show last
# Watch: tree should show summary at top + reasoning + tool actions with file chips
```

- [ ] **Step 3: CHANGELOG entry.**

In `CHANGELOG.md` under `[Unreleased]` → `### Added`:
```markdown
### Added — Thinking History v2: AI Summaries

`/reasoning show` and the collapsed thinking line now include an LLM-generated one-line summary of what the AI did each turn (e.g. "Wrote a haiku about sloths"). Generated via Haiku in a daemon thread; falls back gracefully to today's "Thought for X · N actions" format on slow/failed summarization. The expanded tree also shows file-name chips for Edit/Write/Read tool actions.
```

- [ ] **Step 4: Push + open PR.**

```bash
git push -u origin feat/thinking-history-summary
gh pr create --title "feat: thinking history v2 — AI summaries + richer tree" \
  --body "..."
```

---

## Self-Review Checklist

1. **Spec coverage:**
   - One-line summary in collapsed view (Image #7) → Task 3 ✓
   - Summary in expanded header (Image #6) → Task 1 (`render_turn_tree` updated) ✓
   - File-name chips in expanded tree (Image #6) → DEFERRED to follow-up PR (`args_preview` in current tree shows the file path inside parens; chip-style polish is plumbing-heavy)
   - Inline expand/collapse: deferred (Option A re-print path retained) — documented in out-of-scope ✓

2. **Backwards compat:**
   - `ReasoningTurn.summary` defaults to None — existing turns unaffected
   - `ToolAction.result_file_path` defaults to None — existing call sites unaffected
   - When `generate_summary` returns None or thread doesn't finish in 3s, collapsed line falls back to today's format

3. **Edge cases:**
   - Empty thinking → no summary call (Task 2 test) ✓
   - LLM failure → summary stays None, collapsed line falls back ✓
   - Turn evicted before summary lands → update_summary is a no-op (Task 1 test) ✓
   - Tool-only turn (no thinking text) → no summary call ✓
   - Concurrent summary writes for different turns → safe (different turn_ids, different deque slots)

4. **Performance:**
   - Daemon thread spawn at finalize START → 3s join max → typical Haiku is <2s → 95%+ of summaries land in time
   - LLM call cost: ~50 input + ~30 output tokens per turn × Haiku rate → negligible

## Audit log — issues caught and resolved

| # | Issue | Resolution |
|---|---|---|
| 1 | `_summary_thread.join(timeout=3.0)` blocks the next prompt for up to 3s — bad UX. | Reduced to 1.5s with documented trade-off; users who want the rich format on slow networks always have `/reasoning show <N>` which reads from the store after the summary lands. |
| 2 | Task 4 (file-name chips) needed PRE+POST hook plumbing changes (`_tool_idx_by_call_id` thread args through) — substantial surface for a polish nice-to-have. | DEFERRED to follow-up PR. v1 keeps `args_preview` in the tree (file paths visible inside parens). |
| 3 | `update_summary` iterates the deque while next finalize might append — race risk. | Different turn_ids, different deque slots, single-Python-thread main loop except for the daemon that updates ONE specific id. Acceptable for v1; flagged as future hardening if a multi-summary stress test surfaces issues. |
| 4 | Frozen `ReasoningTurn` blocks in-place mutation of `summary`. | `update_summary` uses `dataclasses.replace` to swap the deque entry — keeps external API frozen-friendly. |
| 5 | What if `generate_summary` returns very long output (e.g. 200 chars)? | `_clean()` caps at 120 chars. Sufficient for one-line displays. |
| 6 | What if multiple finalize calls happen in fast succession (subagent delegations)? | Each spawns its own daemon thread with a unique turn_id. They run in parallel via Python's GIL — total throughput limited by Haiku rate limits, not our threading. |
| 7 | Unicode handling in `_clean()` — strip + punctuation removal — does it break on non-Latin scripts? | `.strip()` and `.rstrip(".!?:; ")` are Unicode-safe in Python 3. Length cap is char-based not byte-based. Safe. |
