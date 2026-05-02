# Anthropic Opus 4.7 Migration + Stop-Reason Hygiene

**Date:** 2026-05-02
**Scope:** Subsystem A from the docs-1-7 review. Provider hygiene + thinking-shape migration + stop-reason coverage.
**Status:** Design — pending user review.

---

## 1. Problem

`claude-opus-4-7` is the default model in [extensions/anthropic-provider/provider.py:173](../../extensions/anthropic-provider/provider.py). Three live bugs and four silent failures sit on top of that default:

| # | Issue | Effect on user |
|---|---|---|
| 1 | [provider.py:413](../../extensions/anthropic-provider/provider.py) hardcodes `temperature: float = 1.0`. Opus 4.7 rejects `temperature`, `top_p`, `top_k` with HTTP 400. | Every chat call 400s on the default model. |
| 2 | [runtime_flags.py:56](../../opencomputer/agent/runtime_flags.py) emits `thinking: {"type": "enabled", "budget_tokens": N}`. Opus 4.7 rejects this with HTTP 400. | `/reasoning` is broken on the default model. |
| 3 | Opus 4.7 defaults `display` to `"omitted"`. We never override it. The streaming loop at [provider.py:633](../../extensions/anthropic-provider/provider.py) listens for `thinking_delta` events that never fire. | PR #266's Thinking Dropdown silently empty on Opus 4.7. |
| 4 | [loop.py:2753](../../opencomputer/agent/loop.py) `stop_reason_map` only covers `end_turn`/`tool_use`/`max_tokens`/`stop_sequence`. | Refusals get silently mapped to `END_TURN` — user sees nothing. |
| 5 | No handler for `model_context_window_exceeded` (Sonnet 4.5+ stop reason). | Context-full responses look like spontaneous truncation. |
| 6 | No empty-`end_turn` detection. Per Doc 3, Claude can return 2-3 empty tokens after tool results when text is appended in the same content block. | The agent appears to "hang" or "ignore" the user. |
| 7 | No `max_tokens` + incomplete-`tool_use` retry. | Truncated tool calls become runtime errors when dispatcher sees a tool with missing arguments. |

## 2. Goals & non-goals

**Goals:**

- Restore Opus 4.7 as a working default.
- Populate the existing Thinking Dropdown when `/reasoning` is on.
- Surface refusals and context-window exhaustion to the user instead of swallowing them.
- Auto-recover from truncated tool calls.

**Non-goals (deferred to subsequent PRs):**

- Per-mode/persona/subagent effort policy (Subsystem B).
- Structured outputs / strict tool use (Subsystem C).
- Context Editing / Token Counting / batch processing (Subsystems D, E).
- OpenAI provider changes — its `reasoning_effort` mapping in `runtime_flags.py` already maps cleanly and is unaffected.

## 3. Approach — model-capability helper + targeted patches

A single small pure-function module exposes the two questions every provider needs to answer per request:

```python
# opencomputer/agent/model_capabilities.py
def supports_adaptive_thinking(model: str) -> bool: ...
def supports_temperature(model: str) -> bool: ...
def thinking_display_default(model: str) -> str: ...
```

Detection is allow-list-based with a forward-compatible default. Concrete table:

| Model name pattern | `adaptive` | `temperature` | `display` default |
|---|---|---|---|
| `claude-opus-4-7*`, `claude-mythos*`, `claude-opus-4-8*` and forward | ✅ | ❌ (not supported per Doc 4 for 4.7; treated unverified-strict for Mythos) | `"summarized"` (we set it explicitly because the API default is `"omitted"`) |
| `claude-opus-4-6*`, `claude-sonnet-4-6*` | ✅ (recommended) | ✅ | `"summarized"` (already the default but we set it explicitly so behavior doesn't drift if Anthropic flips the default) |
| `claude-opus-4-5*`, `claude-sonnet-4-5*`, `claude-haiku-4-5*`, `claude-sonnet-3-7*`, `claude-haiku-3*`, anything older | ❌ (uses legacy `enabled+budget_tokens`) | ✅ | n/a — legacy block shape, no `display` field |
| Unknown `claude-*` model | ✅ default-forward | ❌ default-forward | `"summarized"` |
| Unknown non-claude model | ❌ | ✅ | n/a |

Forward-compatible default rationale: Anthropic's trajectory is everything moves to adaptive + temperature-removed. Defaulting unknown future models to that shape avoids 400s on the day a new model lands. Worst case: a regression we then patch.

Why Mythos lands in the no-temperature bucket: Doc 5 lists Mythos as effort-supported and adaptive-default but is silent on temperature. Conservative read: if Anthropic's pattern is "newer = no temperature", default Mythos to that pattern. If wrong, the fix is one line in the capability table.

### 3.1 Provider patches

In [extensions/anthropic-provider/provider.py](../../extensions/anthropic-provider/provider.py):

```python
# In _do_complete / _do_stream_complete kwargs construction:
kwargs = {"model": model, "max_tokens": max_tokens, "messages": api_messages}
if supports_temperature(model):
    kwargs["temperature"] = temperature
if sys_for_sdk:
    kwargs["system"] = sys_for_sdk
if tools:
    kwargs["tools"] = [t.to_anthropic_format() for t in tools]
```

Drop the unconditional `temperature=temperature`. Same change in both code paths (sync `_do_complete` and streaming `_do_stream_complete` and the `stream_complete` async generator).

### 3.2 runtime_flags.py migration

Replace `_ANTHROPIC_REASONING_BUDGET` (token-budget table) with effort-string passthrough, then branch on `supports_adaptive_thinking(model)`:

```python
# New signature — needs model so we can pick the shape
def anthropic_kwargs_from_runtime(
    *, model: str, reasoning_effort: str | None = None,
    service_tier: str | None = None,
) -> dict:
    out: dict = {}
    if reasoning_effort and reasoning_effort != "none":
        if supports_adaptive_thinking(model):
            out["thinking"] = {"type": "adaptive", "display": "summarized"}
            out["output_config"] = {"effort": _map_effort(reasoning_effort)}
        else:
            budget = _LEGACY_BUDGET.get(reasoning_effort)
            if budget is not None:
                out["thinking"] = {"type": "enabled", "budget_tokens": budget}
    if service_tier == "priority":
        out["service_tier"] = "priority"
    return out
```

`_map_effort` collapses our internal scale (`minimal`/`low`/`medium`/`high`/`xhigh`/`max`) onto Anthropic's effort values (`low`/`medium`/`high`/`xhigh`/`max`). `minimal` → `low`; everything else passthrough including `max` (rare today, but if a future slash command sets it, we don't want silent drop). Unknown values fall back to `high` (the API default).

Threading `model` requires an upstream change: the two call sites in [provider.py](../../extensions/anthropic-provider/provider.py#L440) (`_do_complete`, `_do_stream_complete`) already have `model` in scope — pass it through:

```python
kwargs.update(
    anthropic_kwargs_from_runtime(
        model=model,
        reasoning_effort=runtime_extras.get("reasoning_effort"),
        service_tier=runtime_extras.get("service_tier"),
    )
)
```

Three call sites total. Tests for `runtime_flags.anthropic_kwargs_from_runtime` will need updating — they currently pass no `model` kwarg.

### 3.3 Stop-reason map extension in loop.py

[loop.py:2753](../../opencomputer/agent/loop.py) currently:

```python
stop_reason_map = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.END_TURN,
}
```

New cases need new `StopReason` enum values + handlers:

- `refusal` → new `StopReason.REFUSAL`. Loop preserves any model-emitted refusal text (Anthropic sometimes returns a brief explanation alongside `stop_reason=refusal`) and prepends a system-italicized note ("_Claude declined to respond._") so the user can tell apart "model said nothing" from "model declined." Adds `REFUSAL` to `plugin_sdk/core.py:StopReason` AND re-exports it from `plugin_sdk/__init__.py:__all__` (BC rule #4 — additive only).
- `model_context_window_exceeded` → new `StopReason.CONTEXT_FULL`. Loop emits a visible status line ("_Context full — compressing…_"), calls `CompactionEngine.maybe_run(messages, last_input_tokens, force=True)`, then retries the same turn with the compacted message list. If the retry returns the same stop reason, surface as `CONTEXT_FULL` with a clear "compaction insufficient — please start a new session" line.
- Empty `end_turn` — stop_reason=`end_turn` AND `not msg.content` AND `not msg.tool_calls` AND `not msg.reasoning`. Reasoning-only responses (model thought but emitted no output text) are not "empty" — those legitimately end the turn. The retry path: build a one-shot synthetic `wire_messages` with a `{"role": "user", "content": "Please continue."}` appended **for the retry call only**. The synthetic user message is NOT persisted to SessionDB — it exists only in the API request. If the retry is still empty, surface as a normal empty turn (no infinite loop).
- `max_tokens` AND last block is `tool_use` — retry with `max_tokens * 2`, capped via `model_metadata.context_length(model)` if available else hardcoded 64000. The cap is further reduced to 32000 for non-streaming requests on Opus 4.7 (Anthropic's API rejects max_tokens > 64k non-streaming on Opus 4.7; halving headroom protects against 400). Streaming-vs-non-streaming is detectable in the provider call site. If retry still truncates, surface as a normal `MAX_TOKENS` outcome.

Each retry is one-shot — no nested loops. Gated by a per-turn retry counter in `StepOutcome` so the loop's outer iteration limit catches runaway cases. The retry counter is observable in tests.

### 3.4 max-output-tokens floor for adaptive models at high effort

Per Doc 5: "When running Claude Opus 4.7 at `xhigh` or `max` effort, set a large `max_tokens` so the model has room to think and act across subagents and tool calls. Starting at 64k tokens."

Today [config.model.max_tokens](../../opencomputer/agent/config.py) is a flat default. Add a small floor lift inside `_do_complete` and `_do_stream_complete`:

```python
if supports_adaptive_thinking(model) and runtime_extras and runtime_extras.get("reasoning_effort") in ("high", "xhigh", "max"):
    streaming_call = self._is_streaming_path  # set by stream_complete; defaults False on _do_complete
    cap = 128_000 if streaming_call else 64_000
    max_tokens = max(max_tokens, min(64_000, cap))
```

The cap respects Anthropic's API limits: 64k non-streaming, 128k streaming on Opus 4.7. We use 64k as the floor in both cases (above the typical default of 4-8k, well below the 128k streaming ceiling). Users get more headroom only when they asked for high reasoning. Cost-visible: bounded by actual output tokens (not max_tokens), so no extra cost unless the model uses the headroom.

## 4. Components & file map

| File | Change |
|---|---|
| `opencomputer/agent/model_capabilities.py` | NEW — pure-function capability table (~80 LOC). |
| `opencomputer/agent/runtime_flags.py` | Add `model` parameter, branch on `supports_adaptive_thinking`, replace token-budget table with effort string mapping. |
| `extensions/anthropic-provider/provider.py` | Conditional `temperature` insertion (3 sites — `_do_complete`, `_do_stream_complete`, `stream_complete`). `display: "summarized"` is set inside `runtime_flags.anthropic_kwargs_from_runtime` and lands in `kwargs["thinking"]` automatically. max_tokens floor lift on high-effort calls. |
| `opencomputer/agent/loop.py` | `stop_reason_map` extension. New StepOutcome retry-counter field. Empty-`end_turn` and `max_tokens`+`tool_use` retry handlers — both inline before the map. Refusal + context-full handlers after the map. Synthetic-continuation prompt is wire-only (not persisted). |
| `plugin_sdk/core.py` | New `StopReason` enum values: `REFUSAL`, `CONTEXT_FULL`. Additive only — no existing values change. Re-exported via `plugin_sdk/__init__.py:__all__` (already lists `StopReason`, no change needed there since it re-exports the enum class). |
| `tests/test_runtime_flags.py` | Update existing tests to pass `model`. Add coverage for adaptive vs legacy branches. |
| `tests/test_anthropic_provider.py` (or equivalent) | Coverage for temperature drop on Opus 4.7, display=summarized on adaptive models, max_tokens floor lift. |
| `tests/test_loop_stop_reasons.py` | NEW — coverage for refusal, context-full retry, empty-end_turn retry, max_tokens+tool_use retry. ~5 tests. |

Total: 1 new module, 3 modified, 2 new test files, 1 modified test file.

## 5. Data flow

```
turn → AgentLoop._step
       ↓
       runtime_extras = {reasoning_effort, service_tier}  (from /reasoning, /fast)
       ↓
       provider.complete(model=…, runtime_extras=…)
       ↓
       runtime_flags.anthropic_kwargs_from_runtime(model=…, reasoning_effort=…)
                ├── supports_adaptive_thinking(model)? → adaptive + display=summarized + effort
                └── else → legacy enabled+budget_tokens
       ↓
       provider drops temperature/top_p/top_k if not supports_temperature(model)
       ↓
       provider.client.messages.create(...)
       ↓
       response.stop_reason
       ↓
       loop.py inline handlers (empty-end_turn / max_tokens+tool_use) — retry once if matches
       ↓
       stop_reason_map → StopReason enum
       ↓
       loop.py post-map handlers (refusal / context_full)
```

## 6. Error handling

- **400 from API** (any cause) — already raised by SDK and propagated; no change.
- **Retry-once contract** — every new retry path is one-shot. Re-entry returning the same trigger surfaces the original outcome.
- **Compaction-during-context-full failure** — if `CompactionEngine` fails (e.g., tool_use/tool_result split error per CLAUDE.md gotcha #3), surface as `CONTEXT_FULL` to user with the compaction error in the visible message.
- **Empty-end_turn after retry** — accept the empty turn rather than recursing. Prevents pathological loops on users who deliberately end turns.

## 7. Testing strategy

- **`model_capabilities.py`** — exhaustive table-driven tests across every claude-* model name we know about + 3 unknown future-fakes (`claude-opus-4-9-20271101`, `claude-mythos-2026`, `claude-orion-future`).
- **`runtime_flags.anthropic_kwargs_from_runtime`** — 6 tests: adaptive emits adaptive shape; legacy emits enabled+budget; effort `none` emits nothing; xhigh passes through to adaptive; minimal collapses to `low` on adaptive and `1024` budget on legacy; service_tier still works.
- **provider.py** — 4 tests: Opus 4.7 call has no `temperature` kwarg; Opus 4.5 call has `temperature` kwarg; adaptive + high effort lifts `max_tokens` to 64k; non-adaptive doesn't.
- **loop.py stop reasons** — 5 tests using a stub provider:
  - Refusal stop_reason produces user-visible message
  - `model_context_window_exceeded` triggers compaction + retry once
  - Empty `end_turn` after tool_result triggers continuation prompt + retry once
  - `max_tokens` with last tool_use block triggers max_tokens-doubled retry
  - All four retry-once contracts respected (no infinite loop on stuck triggers)

Total ~17 tests. ~10 new, ~7 updated.

## 8. Rollout

- One PR off `main` in a fresh worktree branch named `feat/opus-4-7-migration`.
- Per memory rule, do not work on `p1c-clean` (active provider catalog branch).
- CI gates: existing pytest + ruff. Memory rule "no push without deep testing" applies — full suite runs locally before push.
- Manual smoke: `oc` chat on Opus 4.7 (default), confirm no 400. `/reasoning high` then any prompt → confirm thinking dropdown populates. Force a refusal-prone prompt → confirm visible message rather than empty response.

## 9. What this does NOT change

- OpenAI provider behavior — untouched. `reasoning_effort` already passes through correctly.
- Default `temperature` for non-Opus-4.7 models — unchanged.
- The `/reasoning`, `/fast`, `/compress` slash commands' UX — unchanged. Only the kwargs they generate change.
- Subagent / voice / Sonnet-chat effort defaults — explicitly out of scope (Subsystem B).

## 10. Open questions resolved

- **Auto-retry on `model_context_window_exceeded`** — yes, with visible status. Confirmed by user.
- **Auto-fallback to Haiku 4.5 on `refusal`** — no. Surfacing to user is the conservative call. Auto-fallback is a behavioral change that warrants its own design.
- **Worktree** — yes, fresh branch off `main`.

## 11. Acceptance criteria

1. `oc` chat default-on-Opus-4.7 makes a successful round-trip with no 400 errors.
2. `oc` then `/reasoning high` then any prompt → live thinking text streams into the dropdown panel.
3. A prompt designed to trigger a safety refusal produces a visible "Claude declined…" message rather than an empty assistant turn.
4. A 200k-token-stuffed conversation produces a visible "Context full, compressing…" line followed by a clean completion.
5. Full pytest suite green. Ruff clean.
