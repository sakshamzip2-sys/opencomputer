# AUDIT — Model-Agnosticism Plan (2026-04-29)

**Verdict:** YELLOW. One critical defect (B's premise is wrong); rest are fixable in-line. Plan is salvageable with a Phase-0-style verification pass.

Evidence taken from: `extensions/openai-provider/provider.py`, `extensions/anthropic-provider/provider.py`, `plugin_sdk/provider_contract.py`, `opencomputer/agent/loop.py`, `opencomputer/cli_ui/slash.py`, `opencomputer/cli_ui/slash_handlers.py`, `opencomputer/cli.py`.

---

## 1. CRITICAL defects

### C1. Anthropic reasoning is NOT already plumbed — plan B's premise is wrong

- **Where:** Sub-project B intro + final integration step.
- **Defect:** Plan asserts *"For Anthropic, reasoning is already populated from `thinking` blocks"*. Reading `extensions/anthropic-provider/provider.py:353-381` (`_parse_response`):
  - Line 355-361: parses content blocks into `text_parts` (text only) and `tool_calls`.
  - Line 373-376: builds Usage with `input_tokens`, `output_tokens`. **No `cache_read_tokens`, no `cache_write_tokens`.**
  - Line 377-381: returns `ProviderResponse(message=..., stop_reason=..., usage=usage)` — **no `reasoning=` parameter.**
  - Search `grep "reasoning\|thinking" extensions/anthropic-provider/provider.py` → `text` blocks parsed at line 358-359; nothing inspects `block.type == "thinking"`.
- **Why it matters:** The plan's "make OC's reasoning surface provider-agnostic" claim only works if BOTH providers populate it. Anthropic doesn't today. Shipping just Sub-project B (OpenAI extraction) leaves Anthropic still unplumbed — partial fix.
- **Fix:** Sub-project B grows by one task — add `_extract_anthropic_reasoning(content_blocks)` that walks blocks for `block.type == "thinking"` and joins their `.thinking` field. Wire into `_parse_response`. ~20 LOC. Same shape as the OpenAI helper.

### C2. Anthropic cache tokens also unplumbed — plan A is half a fix

- **Where:** Sub-project A (assumed Anthropic already populated cache_read_tokens).
- **Defect:** `extensions/anthropic-provider/provider.py:373-376` — `Usage(input_tokens=..., output_tokens=...)` only. Anthropic SDK exposes `usage.cache_creation_input_tokens` and `usage.cache_read_input_tokens` for prompt-cached requests. Plan A only fixes the OpenAI side and silently leaves Anthropic at 0/0 for cache fields too.
- **Why it matters:** Sub-project A's stated goal is "consistent across providers." Without Anthropic, it's just OpenAI parity.
- **Fix:** Add a parallel sub-task to Sub-project A — populate `cache_read_tokens` from `usage.cache_read_input_tokens` and `cache_write_tokens` from `usage.cache_creation_input_tokens` in the Anthropic provider's `_parse_response`. ~5 LOC.

---

## 2. HIGH-priority concerns

### H1. SLASH_REGISTRY mutation pattern — same gotcha caught last chain

- **Where:** Sub-projects D Task D2, Step 1.
- **Defect:** Plan D2 step 1 says "append to SLASH_REGISTRY" via `SLASH_REGISTRY.append(CommandDef(...))`. **Confirmed `_LOOKUP` is built once at module import** (`slash.py:148`). Appending after import does NOT update `_LOOKUP`. Plan must add the entry to the SLASH_REGISTRY literal in `slash.py` directly, not via `.append()` from elsewhere.
- **Fix:** Same pattern as `/debug` (PR #257 / Sub-project D from ship-now). Edit the literal block at `slash.py:44-128`.

### H2. AgentLoop.config reassignment safe (no slots)

- **Where:** Sub-project C Task C2 (mutate `agent_loop.config` from closure).
- **Verification:** `agent/loop.py:231` shows `__init__`. `grep "__slots__" loop.py` returned nothing. AgentLoop has no `__slots__`, so `agent_loop.config = dataclasses.replace(...)` works fine.
- **Status:** No defect — confirmed safe.

### H3. SlashContext built per-iteration, not once

- **Where:** `cli.py:1333-1334` — `SlashContext(...)` is constructed INSIDE the chat loop's per-prompt branch (`if is_slash_command(user_input):`).
- **Implication:** The `_on_model_swap` closure captures `agent_loop` by name. Since `agent_loop` is a stable instance in the chat loop's outer scope, closure captures it correctly. **Status: safe.** But the test pattern in C2 ("reuse fixtures from tests/test_agent_loop.py") is abstract — engineer needs the actual fixture pattern (which exists).

### H4. OpenAI `prompt_tokens_details` may not exist on stream chunks

- **Where:** Sub-project A Task A2, lines 403/518 (stream paths).
- **Concern:** OpenAI streaming returns `chunk.usage` only on the final chunk (with `stream_options={"include_usage": True}`). Plan's helper handles `None` usage gracefully — verified safe with `_extract_cached_tokens(None) -> 0` test.
- **Status:** Safe-by-design (helper returns 0 on None/missing). No defect.

### H5. `chunk.choices[0].delta.reasoning_content` is non-standard SDK

- **Where:** Sub-project B Task B2 step 2 (streaming reasoning accumulation).
- **Concern:** OpenAI's official `ChoiceDelta` type doesn't include `reasoning_content` — it's a vendor-specific extension surfaced as `delta.__pydantic_extra__["reasoning_content"]` or via `model_extra`. `getattr(delta, "reasoning_content", None)` works for pydantic models with `extra="allow"` (the default in OpenAI SDK >= 1.0). Verified safe for current SDK; may break on a future OpenAI SDK that switches to strict pydantic.
- **Status:** Acceptable for now; document the dependency.

### H6. Sub-project E (Cohere) is still a placeholder

- **Where:** E Task E1 step 3 says "implement based on Cohere's `cohere.AsyncClientV2().chat(...)` API" — that's not a step, that's a 200 LOC subproject with no detail.
- **Recommendation:** **Drop E from this wave.** Document the BaseProvider extension pattern in a `docs/refs/non-openai-compat-providers.md` markdown instead. Cohere as a first-class plugin is a future PR with its own plan.

---

## 3. Stress-test mitigations

- **Both keys + swap mid-session**: Sub-project D's swap mutates `agent_loop.provider`. State carries across (token accounting via SessionDB persists). Cache invalidation: irrelevant — each provider has its own cache state. Acceptable.
- **`/model nonexistent` typed**: closure validates via `resolve_model` (which doesn't validate against provider's model catalog). New model id passes through; turn fails at provider call. Plan should add a soft-validate step: warn if the new id doesn't match any known pattern, but don't block.
- **OpenRouter pass-through headers**: OpenRouter accepts the OpenAI completions API verbatim (uses Bearer auth, no extra headers required by default). The optional `HTTP-Referer` / `X-Title` headers are for OpenRouter analytics — not required for the API to function. Status: safe.

---

## 4. Final verdict

| Pick | Verdict | Rationale |
|---|---|---|
| A | **REVISE** — add Anthropic cache_tokens | C2 defect — Anthropic also unplumbed |
| B | **REVISE** — add Anthropic reasoning extraction | C1 critical defect — Anthropic was NOT already plumbed |
| C | **KEEP** | Pattern verified; no slots/closure issues |
| D | **REVISE** — fix SLASH_REGISTRY mutation pattern | H1 same gotcha as /debug last chain |
| E | **DROP from this wave** | Placeholder; defer to its own plan |

**Overall:** **YELLOW**. Critical fixes are surface-level (~25 LOC additional in Anthropic provider). Drop E and the wave is 4 picks, ~3-4 days. Plan goes GREEN once C1+C2+H1 land in AMENDMENTS.

**Recommended next action:** apply AMENDMENTS, then proceed to execution. Sub-project order (simplest first): A (now expanded to OpenAI+Anthropic) → C (mid-session model swap) → B (reasoning, now expanded) → D (provider swap).
