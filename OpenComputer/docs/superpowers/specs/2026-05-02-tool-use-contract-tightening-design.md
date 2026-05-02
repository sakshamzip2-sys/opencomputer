# Tool-Use Contract Tightening — Design

**Date:** 2026-05-02
**Author:** Claude (audit) + Saksham (review)
**Status:** Draft for plan

## Background

Audit of OpenComputer against the canonical Anthropic tool-use contract (six docs: tool-use overview, tool-call lifecycle, parallel tool use, Tool Runner SDK, `memory_20250818`, `bash_20250124`) surfaced 8 deltas. After applying the "best of the best, only what's required" filter against the existing investment in consent gates, capability claims, layered awareness, profile scoping, and BGE/Chroma recall, this design takes 5 items and explicitly rejects 3.

## Goals

Adopt the parts of the contract that are clearly missing or misallocated **without** disturbing the consent layer, capability claims, or memory architecture.

Concretely: cut input cost, fix two latent bugs, raise tool-call reliability, raise fan-out latency, and reduce model confusion across overlapping memory subsystems.

## Non-goals

- Migration to trained-in `bash_20250124`, `memory_20250818`, `text_editor_20250728`, `computer_20250124` schemas
- Widening `ToolResult.content` to `str | list[ContentBlock]` (no consumer planned in this scope)
- Persistent bash session state (deliberate bounded design)
- Memory subsystem consolidation (5 systems serve distinct purposes)
- Performance benchmarking (post-merge ticket if interest)

## Items

### Item 1 — `cache_control` on tools array

**Problem.** [opencomputer/agent/prompt_caching.py](../../../opencomputer/agent/prompt_caching.py) places 4 ephemeral cache breakpoints on **system prompt + last 3 non-system messages**. The `tools` array (≈8-30k tokens for ~40 registered tools) is sent uncached on every request.

**Change.** Reallocate breakpoints to `tools[-1] + system + last 2 non-system messages`. Total stays at 4 (Anthropic max).

**Rationale.** Tool definitions change rarely (high cache hit rate); the deepest of the 3 message breakpoints is the lowest-hit (every turn changes the tail). Anthropic's caching is prefix-based, so the system breakpoint still anchors the static prefix; the new tools breakpoint extends the cache further.

**Files.**
- [opencomputer/agent/prompt_caching.py](../../../opencomputer/agent/prompt_caching.py) — extend `apply_anthropic_cache_control` to mark the last tool entry; reduce message-tail cache from 3 to 2.
- [extensions/anthropic-provider/provider.py:437,528](../../../extensions/anthropic-provider/provider.py) — both call sites build the tools list via `[t.to_anthropic_format() for t in tools]`. Inject the cache marker on the last element after that comprehension.

**Tests.**
- Update `tests/test_prompt_caching.py` for new allocation (system + last 2, not last 3).
- Add new test asserting `tools[-1]` carries `cache_control: {type: "ephemeral"}` when ≥1 tool registered.
- Add test asserting total breakpoint count stays ≤ 4.

**Acceptance.**
- All existing prompt-caching tests pass after update.
- New tools-cache test passes.
- Manual smoke test: capture `Usage` from a 5-turn conversation; `cache_read_input_tokens` should grow each turn.

---

### Item 2 — `pause_turn` and `refusal` in stop_reason_map

**Problem.** [opencomputer/agent/loop.py:2753-2764](../../../opencomputer/agent/loop.py#L2753-L2764) maps four stop reasons:

```python
stop_reason_map = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.END_TURN,
}
stop = stop_reason_map.get(resp.stop_reason, StopReason.END_TURN)
```

`pause_turn` and `refusal` fall through to `END_TURN` via the default. `pause_turn` means "server tool needs more time, re-send to continue"; defaulting to END_TURN silently truncates the work. `refusal` means the model refused; defaulting hides this from any downstream consumer.

**Change.**
- Add `StopReason.PAUSE_TURN` and `StopReason.REFUSAL` to the enum in [plugin_sdk/core.py:417](../../../plugin_sdk/core.py#L417).
- Map `"pause_turn"` and `"refusal"` to the new values.
- `pause_turn` handling: re-issue the provider call with the conversation including the paused assistant response. Cap consecutive pauses at 3 to avoid infinite loops; on cap exceeded, surface as END_TURN with a warning logged.
- `refusal` handling: surface to user as final assistant text, do NOT retry. Loop exits.

**Files.**
- `plugin_sdk/core.py` — `StopReason` enum addition.
- `opencomputer/agent/loop.py` — map extension, pause-turn re-send logic, refusal surfacing.

**Tests.**
- Unit test: mocked provider returns `pause_turn` then `end_turn`; assert loop continues with prior content preserved.
- Unit test: mocked provider returns `pause_turn` ×4; assert loop exits at cap with logged warning.
- Unit test: mocked provider returns `refusal`; assert loop exits without retry, final assistant text preserved.

**Acceptance.**
- Three new tests pass.
- Existing loop tests unchanged.

---

### Item 3 — `strict: true` on tool definitions

**Problem.** No tool schema declares `strict: true`. This is the doc's recommended way to guarantee tool inputs match the schema (no missing required params, no extra fields, no type mismatches). Without it, malformed calls cost a turn and may trip the consent gate.

**Change.**
- Add `strict_mode: ClassVar[bool] = True` to `BaseTool` in `plugin_sdk/tool_contract.py`.
- Modify `ToolSchema.to_anthropic_format()` to emit `"strict": True` when `strict_mode` is True.
- Audit all 40 tools in `opencomputer/tools/`: any whose schema cannot satisfy strict requirements (no `additionalProperties: false`, missing explicit types) gets `strict_mode = False` with a one-line comment explaining why and a follow-up TODO.
- Goal: ≥80% of tools pass strict on first run. Failing tools are tracked, not blocked.

**Strict requirements per Anthropic:**
- All object schemas need `additionalProperties: false`
- All required fields must be in `required[]`
- No optional union types without explicit `null` handling

**Files.**
- `plugin_sdk/tool_contract.py` — `BaseTool.strict_mode`, `ToolSchema.to_anthropic_format` change.
- `opencomputer/tools/*.py` — per-tool audit pass.

**Tests.**
- New parametrized test loops every registered tool: if `strict_mode = True`, assert schema passes a strict-validation check.
- Snapshot test: API request payload includes `"strict": true` on at least one tool.

**Acceptance.**
- ≥80% of tools have `strict_mode = True` and pass strict-validation test.
- Failing tools have explanatory comments and tracking TODOs.
- Full pytest suite passes.

---

### Item 4 — Parallel-tool system prompt nudge

**Problem.** No system-prompt instruction encourages parallel tool calls. Claude 4 defaults are good but the doc explicitly recommends a stronger nudge for fan-out heavy workflows (multi-file reads, multi-grep, multi-glob).

**Change.** Append the canonical block from the doc to the assembled system prompt:

```
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

The example was lightly adapted: `ls`/`list_dir` from the doc replaced with `Glob`/`Grep` to match OpenComputer's tool names.

**Files.**
- [opencomputer/agent/prompts/base.j2](../../../opencomputer/agent/prompts/base.j2) — append a new top-level section `# Tool-call efficiency` after `# Working rules`, containing the `<use_parallel_tool_calls>` block. Keep it isolated so it can be lifted out cleanly.

**Tests.**
- Snapshot test confirming the block is present in the assembled system prompt.

**Acceptance.**
- Snapshot updated and committed.
- No regressions in existing prompt-snapshot tests.

---

### Item 5 — Memory tool routing description audit

**Problem.** OpenComputer has at least five overlapping memory subsystems:
1. `Memory` tool ([opencomputer/tools/memory_tool.py](../../../opencomputer/tools/memory_tool.py))
2. `Recall` tool ([opencomputer/tools/recall.py](../../../opencomputer/tools/recall.py)) — vector search via BGE/Chroma
3. `SessionSearch` tool ([opencomputer/tools/sessions.py](../../../opencomputer/tools/sessions.py))
4. Layered Awareness reads (life events, personas, learning moments) — accessed implicitly through context, not user-callable tools
5. User's external SQLite memory at `~/.claude/memory/claude_memory.db` — not seen by OpenComputer

The model picks based on tool description tone. With overlapping descriptions, wrong-tool calls are common.

**Change.** Rewrite tool descriptions for `Memory`, `Recall`, `SessionSearch` (and any other user-facing memory tool surfaced in the audit) so each carries an explicit "**when to use this vs alternatives**" clause.

**Routing matrix to enforce in descriptions:**
- `Memory` — ad-hoc fact storage *you* control. Use for "remember that X" / "what did I tell you about Y." NOT for past conversation lookup (use `SessionSearch`) or semantic recall over journal/notes (use `Recall`).
- `Recall` — vector search over your own past notes/journal. Use for "what did I say about X" semantic queries; returns ranked passages.
- `SessionSearch` — exact/fuzzy search over prior conversation transcripts. Use when the user references "last time we talked" or wants to find a specific past message.
- Awareness reads — automatic context, not invokable; the descriptions should NOT mention these (model only sees tools it can call).

**Files.**
- `opencomputer/tools/memory_tool.py` — rewrite description.
- `opencomputer/tools/recall.py` — rewrite description.
- `opencomputer/tools/sessions.py` — rewrite description.
- Possibly `opencomputer/tools/skill.py`, `opencomputer/tools/skill_manage.py` — audit only; rewrite if memory-adjacent.

**Tests.**
- No behavior tests needed.
- Existing schema-shape tests cover the description-edit case (descriptions are plain strings).

**Acceptance.**
- Each touched tool's description starts with a one-sentence purpose, then a bullet list of "use when" / "do not use when," then the parameter list (if any text precedes parameters today).
- Routing matrix above is reflected in the descriptions verbatim.
- Full pytest suite passes.

---

## Test strategy (overall)

- Items 1, 2, 3 each ship with new unit tests as specified.
- Item 4 lands as a snapshot update.
- Item 5 ships with no new tests; existing schema-shape tests catch regressions.
- Full `pytest` run is mandatory before merge per project convention.
- Manual smoke test: 5-turn conversation against the live Anthropic provider with `Usage` capture, asserting `cache_read_input_tokens > 0` from turn 2 onward (verifies Item 1).

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Strict mode fails for some tools | Per-tool opt-out flag, audit pass logs failures, ≥80% target not 100% |
| Cache reallocation hurts long conversations | `Usage` stats give signal; revert if `cache_read_input_tokens` regresses |
| `pause_turn` infinite loop | 3-attempt cap with logged warning |
| System prompt token bloat | Item 1's tool-cache savings vastly outweigh the ~80-token nudge |
| Description rewrites change user-visible tool docs | Descriptions are model-facing only; user CLI surfaces are separate |

## Out of scope

- All "Reject" items from the audit (trained-in tools, persistent bash, `ToolResult` widening, memory consolidation)
- Performance benchmarking
- Per-tool migration tickets for strict-mode failures (filed separately if any)

## Acceptance criteria for the spec as a whole

- All 5 items shipped behind a single PR or a clearly-grouped sequence (decided in plan phase).
- Full pytest suite passes.
- Manual smoke test confirms Item 1's cost win is real.
- No regressions in any existing test.
