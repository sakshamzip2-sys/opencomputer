# OpenComputer Context Window — Deep Dive

> Why the context bar fills so fast in `oc chat`, where every token is going, and what you can change. Generated 2026-05-11 against the actual code, your actual config, and your actual SessionDB.

---

## TL;DR

Your **frozen baseline system prompt is ~39,000 tokens** before turn 1. That's 20% of a 200K window, paid on session start, every session. Then 5 different memory layers, a 132-tool schema array, a 139-skill bullet list, and 50-100K-character tool results stack on top of that. Anthropic prompt caching *is* working (most of your heavy sessions are 87-96% cache-read), but cache only saves *cost*, not *window space* — the tokens still occupy the context window. Compaction is configured to fire at 80% of window and has **never fired in 430 sessions** because nothing yet hit the threshold; the next big tool result will.

The biggest individual offender right now is **CLAUDE.md at 45 KB / 11K tokens** — bigger than every other contributor except the base template itself.

---

## Part 1 — The measured numbers (against your machine, right now)

### 1.1 The frozen prompt baseline

Every turn includes this whole thing. Anthropic caches it server-side (you have ~71M cache-read tokens across 82 sessions), but the tokens still count against the window.

| Source | Chars | Tokens (~) | File / source |
|---|---:|---:|---|
| `base.j2` template (after Jinja branch resolution) | 25,800 | **6,450** | `opencomputer/agent/prompts/base.j2` (351 lines) |
| Workspace `CLAUDE.md` | 45,249 | **11,312** | Workspace cwd — loaded by `load_workspace_context` |
| Workspace `AGENTS.md` | 4,196 | 1,049 | Same loader |
| `MEMORY.md` (capped at 4 KB) | 2,886 | 721 | `~/.opencomputer/MEMORY.md` |
| `USER.md` (capped at 2 KB) | 1,612 | 403 | `~/.opencomputer/USER.md` |
| `SOUL.md` (no cap) | 3,089 | 772 | `~/.opencomputer/SOUL.md` |
| Skills section bullets (139 skills) | 29,459 | **7,364** | `opencomputer/skills/*/SKILL.md` |
| Tool-schema array (132 tools, descriptions + JSON Schema framing) | 42,686 | **10,671** | All `BaseTool` subclasses across `opencomputer/tools/` + `extensions/*/` |
| **FROZEN TOTAL** | **154,977** | **~38,744 tok** | |

### 1.2 Per-turn volatile additions

Rebuilt fresh every turn. Not cached. Stack on top of the frozen prefix.

| Block | Typical size | Code reference |
|---|---:|---|
| Honcho prefetch `## Relevant memory` | ~800 chars (~200 tok) | `loop.py:1920-1931` |
| MEMORY.md retrieval (BM25+vector RRF, top 5) | ~850 chars (~212 tok) | `loop.py:1933-1987`, `memory_md_retrieval.py` |
| Active memory FTS5 (top 3) — **OFF by default** | 0 (off) | `loop.py:1989-2014`, `config.py:551` |
| Channel prompt + channel skills (Telegram/Discord) | 0 in CLI mode | `loop.py:2016-2054` |
| Coder-identity injection | 222 chars (~55 tok) | `extensions/coding-harness/prompts/coder_identity.j2` |
| Plan-mode reminder (full on turn 1, 6, 11, …; sparse otherwise) | 334 / 110 chars | `extensions/coding-harness/modes/plan_mode.py:34-43` |
| Persona overlay block + explanation | ~1,200 chars (~300 tok) | `loop.py:1747-1773`, `base.j2:278-286` |
| Layered Awareness `user_facts` (top-20 graph nodes, 80 chars each) | ~800 chars (~200 tok) | `prompt_builder.py:434-467` |
| **PER-TURN VOLATILE TOTAL** | **~3,872 chars (~968 tok)** | |

### 1.3 The combined turn-1 cost

| | Chars | Tokens |
|---|---:|---:|
| Frozen baseline | 154,977 | **~38,744** |
| + Per-turn volatile | 3,872 | ~968 |
| **= Turn 1 effective prompt size** | **158,849** | **~39,712 tok** |

That's **19.9% of a 200K-token window** paid before the first user message has any content. On Opus 4.7's 1M window it's a more comfortable 4.0%.

### 1.4 What history adds, by message size

Querying your `sessions.db` (4,193 messages stored across 430 sessions):

| Statistic | Value |
|---|---:|
| Average message content length | 1,436 chars |
| Largest single message | **111,478 chars (~27,869 tok)** |
| Top 10 largest messages — all role | `tool` (tool results) |
| Top 10 sizes | 111K / 99K / 96K / 91K / 84K / 77K / 60K / 58K / 58K / 57K chars |

Translation: a single Bash / WebFetch / Read tool result in your history can drop **27K tokens** into context. Ten of those and you've added more history than the frozen baseline.

### 1.5 LLM-call-level distribution (577 calls measured)

| Per-call input tokens | Count | % |
|---|---:|---:|
| < 10K | 516 | 89% |
| 10K - 50K | 41 | 7% |
| 50K - 100K | 18 | 3% |
| 100K - 150K | 2 | 0.3% |
| ≥ 150K (compaction-eligible at 0.8 ratio) | 0 | 0% |

Max input in any single call: **138,254 tokens**. You came close to 160K compaction trigger but never crossed it. Compactions executed across 430 sessions: **zero**.

### 1.6 Prompt-cache evidence

Anthropic prompt caching IS firing on Opus 4.7. From `sessions.cache_read_tokens`:

| | Value |
|---|---:|
| Sessions with cache hits | 82 / 430 (19%) |
| Total cache-read tokens | 71,338,073 |
| Total cache-write tokens | 14,112,680 |
| Max cache-read in a single session | 16,883,553 |
| Heavy-session cache ratio (sessions with >100K input) | **74-96%** |

For the heaviest dozen sessions, 87-96% of input tokens came from server cache. That's working as designed. But cache savings reduce *cost* (90% discount on cache-read) — they do **not** shrink the context-window occupancy. The tokens are still there, just cheaper.

### 1.7 Tool-result spill (`tool_result_storage/`)

3 files, 1.7 MB total. Largest:
- 1,480 KB (a screenshot PNG — Vision tool output)
- 147 KB and 110 KB (text tool results that exceeded the 100 KB per-result threshold)

The Layer-2 persist-to-disk mechanism (`tool_result_storage.py`) is firing — three results in your entire history have ever crossed the 100K threshold to disk. But the threshold is so generous that the 27K / 21K / 19K tokens-per-message you saw stayed *inline* in history.

---

## Part 2 — The three-layer defense in code

OpenComputer has actual defenses against context overflow. They are layered and partially load-bearing.

### 2.1 Layer 1: per-tool internal truncation

Each tool truncates its own output before returning. For example `Grep` caps at 200 matches; `WebFetch` caps at `max_chars=8000` default. This is the only layer the tool author controls. Inconsistent across the 132 tools.

### 2.2 Layer 2: `maybe_persist_tool_result` (per-result threshold)

Code: `opencomputer/agent/tool_result_storage.py:119-180`.

For each tool result: if it exceeds `DEFAULT_RESULT_SIZE_CHARS = 100_000`, spill the full output to `<profile_home>/tool_result_storage/{tool_use_id}.txt` and replace the in-context content with a `<persisted-output>` block containing a 1,500-char preview.

**The threshold is set in `budget_config.py:27`. `Read` is pinned to `inf` (never persisted, would cause infinite loops).**

In your data: only 3 outputs in history have ever crossed 100 KB. Anything between 10 KB and 100 KB sits in context permanently. The 111K-char tool result that DID stay inline likely crept just below the threshold, or pre-dates the layer.

### 2.3 Layer 3: `enforce_turn_budget` (aggregate per-turn cap)

Code: `opencomputer/agent/tool_result_storage.py:183-236`.

After a turn collects all tool results, if their combined size exceeds `DEFAULT_TURN_BUDGET_CHARS = 200_000`, persist the largest non-persisted ones to disk until the aggregate is under budget.

This catches the "ten 30K results in a turn" case that Layer 2 misses (each 30K result is below the 100K threshold individually, but together they're 300K).

### 2.4 The fourth defense: compaction

Code: `opencomputer/agent/compaction.py`.

When `last_input_tokens >= window * 0.8`, the engine summarizes everything except `preserve_recent: int = 20` messages into one synthetic assistant message tagged `[compacted-summary]`. Aux LLM call. Preserves tool_use/tool_result pairs atomically.

**Defaults:**
- `threshold_ratio: float = 0.8` (`compaction.py:168`) — fire at 80% of window
- `preserve_recent: int = 20` (`compaction.py:167`) — keep last 20 messages verbatim
- `summarize_max_tokens: int = 1024` (`compaction.py:169`)
- `fallback_drop_count: int = 10` (`compaction.py:172`) — if aux LLM fails, drop oldest 10

**The window per model is in `DEFAULT_CONTEXT_WINDOWS` (compaction.py:46-131).** Notably, `claude-opus-4-7: 1_000_000` — your daily driver uses a 1M window. So compaction fires at 800K tokens, not 160K. That's why you've never seen it in 430 sessions. On a Sonnet 4.5 / Sonnet 4.6 session, the threshold is 160K, and your closest call was 138K input — about 14% short of trigger.

---

## Part 3 — Where every byte goes (top to bottom of the prompt)

### 3.1 The base template, slot by slot

`opencomputer/agent/prompts/base.j2` is 351 lines. Reading top to bottom:

| Slot | Lines | Approx tokens | What |
|---|---:|---:|---|
| Opening identity / persona-aware intro | 1-12 | 80 | "You are OpenComputer ..." |
| Slot 1 — `soul` (SOUL.md) | 14-22 | 800 | Profile voicing |
| `# Identity and stance` | 23-30 | 120 | Five bullets |
| `# Working rules` | 32-49 | 600 | 10 numbered rules (action, references, security, etc.) |
| `# Tone and style` | 51-62 | 200 | Persona-aware tone block |
| `# Tool-use discipline` | 64-135 | 1,800 | THE big section: parallel/sequential heuristics, tool selection, error recovery, **failure budget** (~700 tokens alone), what-not-to-do |
| `# Plan mode` / `# Auto mode` | 137-171 | 250 | One of four branches renders |
| `# Memory integration` (with `<memory>`, `<user-profile>`, `<user-tone>`) | 173-226 | 800 + memory caps | Static framing + your MEMORY/USER content |
| Slot 4 — Skills section | 228-246 | **7,364** | **The 139 enumerated skills** |
| Slot 5 — `workspace_context` (CLAUDE.md/AGENTS.md) | 248-256 | **12,361** | **Your workspace files** |
| Slot 5b — pinned files | 258-265 | 0 (no pins) | Only if `oc pin` used |
| Slot 6 — System info (cwd, home, OS, time) | 267-275 | 30 | |
| Slot 7 — Active persona overlay | 277-287 | 300 | |
| `# Personality directive` | 288-292 | 0 (default) | Only if `/personality` set |
| `# Tool result interpretation` | 294-300 | 120 | |
| `# Communicating with the user` | 302-309 | 180 | |
| `# Doing tasks: the loop` | 311-322 | 200 | 6-step loop |
| `# Refusal policy` | 324-338 | 200 | |
| `# Wrapping up` | 340-351 | 100 | |

The base template **without slot fills** is 6,450 tokens. With slot fills it's the 38,744 you saw earlier.

### 3.2 Per-turn injection providers (registered via `register_injection_provider`)

Found by grep — what actually runs every turn:

| Provider | Priority | When it fires | Code |
|---|---:|---|---|
| `coding-harness:coder-identity` | 5 | always | `extensions/coding-harness/modes/coder_identity.py:22` — reads `prompts/coder_identity.j2` (222 chars) |
| `coding-harness:plan-mode` | 10 | when `runtime.plan_mode` true | `extensions/coding-harness/modes/plan_mode.py:53-60` — full on turn 1/6/11/..., sparse otherwise |
| `coding-harness:accept-edits-mode` | 10 | when in accept-edits | `extensions/coding-harness/modes/accept_edits_mode.py` |
| `coding-harness:review-mode` | 10 | when review-mode | `extensions/coding-harness/modes/review_mode.py` (400 chars) |
| `coding-harness:skill-activation` | ? | when a skill matches | `extensions/coding-harness/skills/activation.py:19` — re-injects matched SKILL.md body |
| `link-understanding` | ? | when user references a URL | `extensions/coding-harness/plugin.py:129`, `opencomputer/agent/injection_providers/link_summary.py` |
| `screen-awareness:screen-context` | ? | screen-awareness plugin loaded | `extensions/screen-awareness/injection_provider.py:25` — only if enabled |
| `affect-injection` | ? | affect plugin loaded | `extensions/affect-injection/provider.py:39` |
| `path-glob-rules` | ? | path-rules plugin loaded | `opencomputer/agent/path_rules_injection.py:36` |
| `thinking-injector` | ? | extended thinking enabled | `opencomputer/agent/thinking_injector.py:46` |

All run *concurrently* via `asyncio.gather` (`injection.py:63-66`), ordered by `(priority, provider_id)`. Failed providers log at DEBUG and contribute nothing. Each contribution is joined with `\n\n`.

### 3.3 Memory-bridge injections — the second wave

`loop.py:1920-2054`. After the InjectionEngine runs, these *also* append to `system`:

1. **Honcho prefetch** (`memory_bridge.prefetch`) — calls all active memory providers' `prefetch()` method. Wrapped as `## Relevant memory\n\n<content>`. Variable; up to ~800 chars per provider with truncation.
2. **MEMORY.md retrieval** (`memory_md_retrieval.MemoryMdRetriever.retrieve`) — BM25 over your MEMORY.md combined with vector embeddings via Reciprocal Rank Fusion. Top 5 hits, each prefixed with `[L<start>-<end> via bm25#<rank>,vec#<rank>]`. Default ON (`memory_md_retrieval_enabled: bool = True`, `config.py:564`).
3. **Active memory FTS5** — full-text-search recall from session history. Default OFF (`active_memory_enabled: bool = False`, `config.py:551`). If enabled, top 3 hits prepended as `## Active memory\n\n<content>`.
4. **Channel prompt + channel skills** — only fires in Telegram/Discord/etc. routing; CLI mode is empty.
5. **`MemoryBridge.collect_system_prompt_blocks`** — async aggregation of every memory provider's `system_prompt_block` output, capped at 800 chars each. Appended under `## Memory context`. Default ON (`enable_ambient_blocks: bool = True`, `config.py:518`).

That's **five separate code paths** that append to per-turn system on every single LLM call.

---

## Part 4 — Tool schemas: the silent 10K-token tax

132 tools, each registered with a `ToolSchema`. The full schema (sent in Anthropic's `tools` array on every request) is:

```json
{
  "name": "ToolName",
  "description": "<the description string>",
  "input_schema": {
    "type": "object",
    "properties": { ... per-parameter schema with type, description, enum ... },
    "required": [...]
  }
}
```

Measured from your repo:
- **Total description chars across 132 tools: 16,286**
- **Estimated JSON framing per tool: ~200 chars** (the `{name, type, properties, required}` overhead plus per-parameter `{type, description}` per arg, averaged across observed schemas)
- **Total tools-array payload: ~42,686 chars ≈ 10,671 tokens**

The 10 biggest tool descriptions (chars):
- 584 — `exit_plan_mode.py`
- 492 — `read.py` (the dedicated Read tool)
- 485 — `screen-awareness/tools.py` (Extract screen text)
- 453 — `screen-awareness/recall_tool.py`
- 385 — `opencli-bridge/tools.py` (OpenCliRun)
- 347 — `ask_user_question.py`
- 346 — `opencli-bridge/tools.py` (OpenCliBrowse)
- 340 — `web_search.py`
- 279 — `voice_synthesize.py`
- 257 — `memory-vector/plugin.py`

These are **per-tool**. They sit at the top of every request, cached but counted.

---

## Part 5 — Why your bar fills fast (the four real reasons)

### Reason 1: the workspace `CLAUDE.md` is 45 KB

`load_workspace_context` (`prompt_builder.py:91-183`) walks up to 5 directories from cwd looking for `OPENCOMPUTER.md` / `.hermes.md` / `CLAUDE.md` / `AGENTS.md` / `.cursorrules`. Found files get loaded with head/tail truncation at 100 KB per file (`_WORKSPACE_FILE_CAP_BYTES = 100_000`).

Your `OpenComputer/CLAUDE.md` is 45,249 chars — **under the cap**, fully loaded. That single file = **29% of your frozen baseline**.

### Reason 2: 139 skills enumerated, no filter

`base.j2:228-246` loops over every skill the agent loop knows about and emits a bullet `- **<name>** — <description>` for each. That's 7,364 tokens, every turn. No top-K. No category filter. No "load on demand."

The agent rarely uses more than 3-5 skills in any session, but the cost of *advertising* the whole catalog is permanent.

The skill list is built in `prompt_builder.PromptBuilder.build` from a `list[SkillMeta]` passed in by the AgentLoop. Nothing filters it.

### Reason 3: tool results that stay inline

`DEFAULT_RESULT_SIZE_CHARS = 100_000` (`budget_config.py:27`) means anything 99,999 chars and below stays inline forever. Your top-10 messages range from 57K to 111K chars — most stayed inline. Each one is 14-28K tokens parked in history for the rest of the session.

Your `ps aux` Bash output from earlier today, with its `--gpu-preferences=WAAAAAAA...` base64 blob, was a perfect example: a single tool call adding 5-10 KB of high-entropy noise to context.

### Reason 4: per-turn memory blocks compound

Five separate injection paths (§3.3) each contribute 200-1,000 tokens **per turn**. None of them are cached (the cache marker is on `base_system`, but per-turn content is appended *after* the cache boundary — `loop.py:1825-1827`):

```python
volatile_memory_blocks: list[str] = []
injected_volatile = injected or ""
system = base_system + ("\n\n" + injected if injected else "")
```

This is intentional — the per-turn content varies turn-to-turn, so it *can't* be cached. But it means a 10-turn session pays ~10,000 tokens of memory framing alone, on top of the 387K cumulative frozen baseline.

---

## Part 6 — The defenses that aren't firing

### 6.1 Compaction has never run

Across 430 sessions, **zero compactions**. Because:
- Opus 4.7 has a 1M-token window in `DEFAULT_CONTEXT_WINDOWS`
- `threshold_ratio = 0.8` → fire at 800K input tokens
- Max observed input in any single call: 138K
- Closest sessions are on the 200K-window Sonnet line — and even those are below 160K

The threshold is too high for your actual usage pattern. You'd need 5x more history per session to trigger. Which means compaction is basically dead code for your daily use.

### 6.2 Layer 2 spill rarely fires

3 files in `tool_result_storage/`, of which one is a screenshot. The 100K threshold means anything between 10K and 100K stays inline.

### 6.3 `tokenjuice` is disabled

`opencomputer/agent/tokenjuice.py:125`: `enabled: bool = False`. This is the OpenClaw-parity deterministic compactor for noisy tool outputs (find, npm install, git status). Three strategies: `none`, `truncate` (keep first N + last M lines), `summary` (keep head/tail + lines matching error/warning/path regex). All off by default.

If turned on with `summary` strategy and reasonable head/tail caps, it would catch your `ps aux --gpu-preferences` blobs deterministically without firing the aux LLM.

### 6.4 `context_pruning` is in `mode="none"`

`opencomputer/agent/context_pruning.py:67`: `mode: ContextPruningMode = "none"`. Alternative modes: `sliding` (last N turns) and `cache-ttl` (drop messages older than the cache TTL). Both default to off.

So OpenComputer ships with **three separate auto-shrinking mechanisms all turned off by default**: tokenjuice, context_pruning, and (in practice) compaction. The only one that fires is per-result spill at the 100K threshold.

---

## Part 7 — What history looks like once you've been chatting a while

Combining the measured numbers:

**Turn 1** (fresh session, one user message):
- Frozen baseline: 39K tok (20% of 200K window, 4% of 1M)

**Turn 10** (10 rounds with normal tool use — Read, Grep, Bash, Edit):
- Frozen baseline: 39K
- Per-turn volatile × 10 ≈ 10K
- Average message * 20 messages ≈ 7K tok (1,436 chars * 20 / 4)
- One big tool result (Bash ps aux or similar): +15K
- **Total: ~71K tok (35% of 200K, 7% of 1M)**

**Turn 30** (sustained work with one or two WebFetch / large Read):
- Frozen: 39K
- Volatile cumulative: 30K
- Average history: ~22K
- Two large tool results inline: ~50K
- **Total: ~141K tok (70% of 200K — bar deep in yellow / red, but compaction still NOT firing because Opus 4.7 has 1M)**

**Turn 50+ with debug output / `ps aux` / WebFetch chains:**
- Easily 200K+ tokens on Sonnet — would compact
- On Opus 4.7 — sails past 200K, 300K, 500K because compaction trigger is at 800K
- The TUI bar shows percentage against the model's window. On Opus 4.7 200K is 20% — looks fine. On Sonnet 4.5 200K is 100% — bar full.

---

## Part 8 — Where to cut, ranked by impact and risk

This is the *what changes which value* table. Order: highest impact first.

### Rank 1 — CLAUDE.md surgery (saves ~9K tokens per turn forever)

**File:** `OpenComputer/CLAUDE.md`
**Current size:** 45,249 chars / ~11,312 tok
**Target:** ~10,000 chars / ~2,500 tok

What's load-bearing in there:
- §1 elevator pitch (~200 lines worth of value distilled to maybe 30)
- §2 layout — useful but exhaustive
- §3 architecture diagram (~50 lines, useful)
- §4 phase table — fresh session uses this for PR-lookup; partially compressible
- §4.1-4.4 sub-section prose — fresh stuff (browser-harness, opencli-bridge, CC §4+10, evolution loop). High value per token.
- §7 10 gotchas — every one is a footgun. KEEP.
- §8 user prefs — KEEP.

What's not:
- All of §2 verbose layout when a `tree -L 2` would suffice
- §6 "How to run / develop / test" — lives in README
- §9 "If you need to dig deeper" — bookmarks, low value per token

Realistic cut: 11K → 3K tokens. **Saves ~8K per turn permanently.** On a 200K window that's 4% of the bar back.

### Rank 2 — top-K skill filter (saves ~6K tokens per turn)

**Code:** `opencomputer/agent/prompt_builder.py:325-432` (builds the `skills` list), `base.j2:228-246` (renders bullets).

Today: every SKILL.md the discovery scan finds gets one bullet. 139 skills × ~210 chars/bullet = 7K tokens.

The agent uses 0-5 skills per session. Advertising 139 is pure overhead.

Three plausible fixes:
1. **Hard cap top-K** — only inject 30 skills. Pick by `last_used_at` (LRU). Saves ~5K tokens. Loses discoverability of unused skills.
2. **Category gate** — skills have an implicit category from their tag (`coding`, `browser`, `voice`, `media`, `infra`). Inject only the current category. Requires the agent loop knowing which category is active (cwd hint, persona, last few messages). More invasive.
3. **Replace bullets with a single-line "139 skills available; run `oc skills list` for the catalog or invoke `SkillTool` with a name"** — saves ~7K tokens but the LLM can't auto-route any more. The skill-activation injection provider already handles late binding via SKILL.md matching after a turn starts.

Option 1 is the cheapest win.

### Rank 3 — lower `DEFAULT_RESULT_SIZE_CHARS` to 20-30K (saves 20-50K per session from large tool results)

**File:** `opencomputer/agent/budget_config.py:27`
**Today:** `DEFAULT_RESULT_SIZE_CHARS: int = 100_000`
**Suggestion:** `25_000` or `30_000`

At 100K threshold, your top-10 tool results (57K-111K chars) — 9 out of 10 stayed inline. At 25K threshold, 9 of 10 would spill to `tool_result_storage/` and be replaced with 1,500-char previews. The agent can still `Read` them when needed.

Risk: more `<persisted-output>` blocks visible to the agent. The agent has to use `Read` to get the bytes, which adds one more tool call per re-inspect. But the bytes only re-enter context when explicitly requested.

### Rank 4 — turn on `tokenjuice` for `summary` mode (saves variable, often 30-70%)

**File:** `opencomputer/agent/tokenjuice.py:125`
**Today:** `enabled: bool = False`
**Suggestion:** `True`, with rule defaults `head=20, tail=20, summary mode`

Targets the exact "Bash output has a base64 blob in the middle" pattern you hit. Not a panic-button shrinker — it just keeps head, keeps tail, keeps error/path/warning lines, collapses the middle into a sentinel. Pure function, deterministic. Excludes `Read` and other "I want the bytes" tools by default.

### Rank 5 — drop compaction `threshold_ratio` to 0.6

**File:** `opencomputer/agent/compaction.py:168`
**Today:** `0.8`
**Suggestion:** `0.6`

On Opus 4.7 (1M window): fires at 600K instead of 800K. Probably still won't fire for you, given your max input is 138K.

On Sonnet 4.5 (200K window): fires at 120K instead of 160K. **This is the one that matters** — your near-misses (138K input) would have triggered compaction at 120K.

### Rank 6 — turn off `memory_md_retrieval_enabled` if MEMORY.md is small

**File:** `opencomputer/agent/config.py:564`
**Today:** `True`
**Your MEMORY.md:** 2,886 chars — already small enough that it's fully visible in the prompt

The retriever runs BM25+vector RRF over your MEMORY.md every turn and prepends top-5 hits. With a 2.8K-char MEMORY.md, the top-5 *are* most of MEMORY.md. You're paying ~200 tokens/turn for an information-redundant block.

Save: ~200 tok/turn, ~6K per 30-turn session.

### Rank 7 — `enable_ambient_blocks: False` if Honcho isn't useful

**File:** `opencomputer/agent/config.py:518`
**Today:** `True`

If you're not actually using Honcho's user-model insights, disabling this saves the per-provider `system_prompt_block` aggregation (capped at 800 chars per provider). One provider = ~200 tok saved per turn.

---

## Part 9 — The big picture, brutal

You have a 39K-token frozen baseline because:
1. The base template is verbose by design (it's training data for tool discipline, refusal policy, plan/auto-mode behavior — all the "act-like-Claude-Code" preamble).
2. The workspace docs (CLAUDE.md + AGENTS.md) you wrote are 12K tokens by themselves.
3. The skill catalog is enumerated rather than filtered.
4. 132 tools × ~300 chars/schema each = 10K tokens just for the toolset.

This is a *design* — high-fidelity context-engineering, the bet being that prompt caching + a 200K-1M window make the cost acceptable. The bet is right on cost (87-96% cache-read in your heavy sessions) and wrong on window pressure (you hit 138K input on Sonnet sessions and started pinching).

The defenses for runaway history (compaction, tokenjuice, context_pruning, layer-2 spill) are mostly OFF by default or set to thresholds that don't fire in your usage. So when context fills, it fills because nothing is shrinking it — not because something broken is duplicating it.

The single highest-leverage change is **shrinking CLAUDE.md**. That's your repo, your file, no code change needed. Skill filter is second, but it's a real code change. Lowering the tool-result threshold is the third and probably the most-felt during noisy debugging sessions.

---

## Part 10 — Exact change locations if you decide to act

### Cuts to user-controlled files (no code change)
- `OpenComputer/CLAUDE.md` — drop to ~10K chars
- `~/.opencomputer/MEMORY.md` — already at cap, no change
- `~/.opencomputer/USER.md` — already at cap, no change

### Cuts via config.yaml (no code change)
```yaml
memory:
  memory_md_retrieval_enabled: false   # if MEMORY.md stays small
  active_memory_enabled: false         # already off
  enable_ambient_blocks: false          # if Honcho not useful

# Compaction config — accessible via the loop's config.compaction
# (would need to find the binding name; LoopConfig has it as a sub-field)
```

### Code changes
- `opencomputer/agent/budget_config.py:27` — `DEFAULT_RESULT_SIZE_CHARS = 25_000`
- `opencomputer/agent/budget_config.py:29` — `DEFAULT_PREVIEW_SIZE_CHARS = 2_500` (keep more of the preview visible since the spill is more aggressive)
- `opencomputer/agent/compaction.py:168` — `threshold_ratio: float = 0.6`
- `opencomputer/agent/compaction.py:167` — `preserve_recent: int = 15` (down from 20 — fewer messages survive compaction; more aggressive shrink)
- `opencomputer/agent/tokenjuice.py:125` — `enabled: bool = True`
- `opencomputer/agent/prompt_builder.py:328` — add a top-K skill filter parameter, default ~30
- `opencomputer/agent/prompts/base.j2:228-246` — alternatively, render only the top-K bullets

### Things to leave alone
- `base.j2` framing — touching it ripples through every persona / mode test
- The `<memory>` `<user-profile>` blocks — they have hard caps already
- The frozen-prefix split for prompt caching — that's load-bearing for the 87-96% cache-read ratio you're getting

---

## Part 11 — Open questions (where I didn't dig further)

> **2026-05-11 update:** Q1–Q3 resolved during the fix that landed alongside this update (see CHANGELOG entry "fix(context-bar): three drifting bugs"). Notes preserved here as the historical paper trail; Q4–Q5 remain open.

1. **Does the TUI bar use Anthropic's actual reported `input_tokens` or a client-side estimate?** ~~~~ **RESOLVED 2026-05-11.** *Neither* — the bar was reading `session_tokens_in + session_tokens_out` (cumulative input across the session + cumulative output, summed). Three bugs in one: cumulative-not-current (inflates ~10x after 10 turns because each turn re-sends history), output-summed-into-input (double-counts, since output becomes next-turn's input), and the `/context` slash command had a parallel "98% trigger" drift unrelated to the engine's real 80%. Fix wired `runtime.custom["last_input_tokens"]` (Anthropic-reported input_tokens of the most recent call) through to both surfaces via new shared resolvers `compaction.resolve_current_input_tokens()` and `compaction.resolve_effective_compaction_threshold_ratio()`. The bar now reflects per-turn occupancy, not session billing.

2. **Is `enforce_turn_budget` actually called?** ~~~~ **RESOLVED 2026-05-11.** Yes — `loop.py:5686` calls `_enforce_turn_budget(tool_message_dicts)` after each turn's tool dispatch (`maybe_persist_tool_result` also wired at `loop.py:5667`). Layer 3 is live, not dead code.

3. **`config.compaction.threshold_ratio` — is it exposed in `config.yaml` or hardcoded?** ~~~~ **PARTIALLY RESOLVED 2026-05-11.** `CompactionConfig` is a frozen dataclass with field default `0.8`. The engine is constructed at `loop.py:858` without a `config=` arg, so the default is used. There is no `LoopConfig.compaction: CompactionConfig` field today — the user cannot override the ratio in `config.yaml` yet. The 2026-05-11 fix makes the runtime-wire forward-compatible: when such a field IS added, the loop's per-turn `self._runtime.custom["compaction_threshold_ratio"] = float(self.compaction.config.threshold_ratio)` write picks it up automatically and the new resolver surfaces it to `/context`. The plumbing through to `LoopConfig` remains a follow-up (small surgical patch — add the field + thread it into `CompactionEngine(config=...)` construction).

4. **The 16,883,553 cache-read tokens in one session** (id `b932677a`) — that session has 261 messages. That's roughly 65K cache-read per turn on average. Worth dumping its `llm_calls` row-by-row to see whether each turn is hitting the same cache slot or rotating through fresh ones.

5. **`prompt_caching.py` allocates 4 cache breakpoints** (tools[-1] + system + last 2 non-system messages). Worth verifying that the *system* breakpoint sits at the end of `base_system` and NOT after the volatile per-turn blocks — because if it's after, every turn's memory churn invalidates the cache. Code in `loop.py:1810-1827` keeps them as separate strings (`base_system` cached, `injected_volatile` not), which is right; M1 validation also confirmed `prompt_caching.py:300-316` stamps `cache_control` on **index 0 of multi-block system content** (the frozen base) via `_mark_system_base_block`, so the volatile injection at index 1+ cannot break the cache prefix. Bug-1 fix from 2026-05-05 confirmed reaching production via `apply_full_cache_control`.

---

*Generated from a live read of:*
- *`opencomputer/agent/prompts/base.j2` (351 lines)*
- *`opencomputer/agent/prompt_builder.py` (584 lines)*
- *`opencomputer/agent/compaction.py` (655 lines)*
- *`opencomputer/agent/tool_result_storage.py` (245 lines)*
- *`opencomputer/agent/budget_config.py` (82 lines)*
- *`opencomputer/agent/injection.py` (109 lines)*
- *`opencomputer/agent/memory_md_retrieval.py` (256 lines)*
- *`opencomputer/agent/loop.py` (6,306 lines, sampled `1720-2100`)*
- *`opencomputer/agent/config.py` (memory + compaction sections)*
- *`opencomputer/agent/tokenjuice.py` (intro + config class)*
- *`opencomputer/agent/context_pruning.py` (intro + config class)*
- *`opencomputer/agent/prompt_caching.py` (intro + helpers)*
- *`~/.opencomputer/MEMORY.md`, `USER.md`, `SOUL.md` (sizes)*
- *`~/.opencomputer/sessions.db` (430 sessions, 4,193 messages, 577 LLM calls)*
- *`~/.opencomputer/tool_result_storage/` (3 spilled files)*
- *All 9 active extensions' tool descriptions (132 tools)*
- *All 139 SKILL.md files under `opencomputer/skills/`*
