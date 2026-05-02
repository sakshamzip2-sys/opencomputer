# Provider-Agnostic Context Economy

**Date:** 2026-05-02
**Status:** Design — pending review
**Author:** Claude (Opus 4.7) for Saksham
**Related audits:** Anthropic context-windows, compaction, and prompt-caching docs (May 2026)

## 1. Problem

OpenComputer talks to many model providers (Anthropic, OpenAI, Gemini through OpenRouter, raw OpenRouter, etc.) but its agent loop and provider integrations have drifted in three ways:

1. **Reasoning continuity bug.** The Anthropic provider's `_to_anthropic_messages()` strips reasoning content on resend. Anthropic's Messages API requires the exact thinking block (with signature) accompanying a `tool_use` request to be returned alongside the corresponding `tool_result`, or reasoning continuity breaks. On Opus 4.5+ and Sonnet 4.6+ this also forfeits free thinking-block preservation in the prompt cache.
2. **Cache observability gap.** The Anthropic provider extracts `cache_creation_input_tokens` and `cache_read_input_tokens` into the canonical `Usage` object, but those fields never surface in `/usage`, audit logs, or `StepOutcome`. Other providers expose analogous fields (OpenAI: `prompt_tokens_details.cached_tokens`; Gemini: `cached_content_token_count`) that we don't read at all. Users have no view of what prompt caching is saving or costing them.
3. **Caching-knob drift.** `prompt_caching.py` always marks the rolling last-3 non-system messages with `cache_control` regardless of model thresholds (silent no-ops below 4096 tokens for Opus, 2048 for Sonnet 4.6, 1024 for older models). The `1h` TTL is wired in `_apply_cache_marker()` but the provider never passes the `cache_ttl` kwarg, so long idle sessions always pay full re-prefill at next turn.

The right shape is provider-agnostic. We currently branch on provider name in several places; we should branch on **capabilities** that each provider declares.

## 2. Goals

- Fix the thinking-block resend bug correctly (per Anthropic's spec) without hard-coding the fix only in the Anthropic provider.
- Make cache hit/miss tokens visible to the user across every provider that exposes them.
- Apply caching micro-optimizations (size threshold, idle-aware TTL) wherever the provider supports them.
- Build the right abstraction (`ProviderCapabilities`) so the next provider we add (Gemini native, Cerebras, etc.) gets these features by declaration, not by patching the loop.

## 3. Non-goals (explicit "do not build")

The following gaps exist but do not earn implementation in this work:

- **Anthropic server-side compaction beta** (`compact_20260112`). Our `CompactionEngine` already integrates with Layered Awareness; the marginal round-trip savings does not justify a beta-API lock-in. Revisit if `/usage` data shows compaction is materially costly.
- **Pre-warming with `max_tokens=0`.** No measured first-turn latency complaint.
- **Token-counting API for budget pre-checks.** Response `Usage` is sufficient.
- **Migrating to Anthropic's automatic top-level caching.** Pure churn; zero behavior change vs the existing rolling-window explicit breakpoints.
- **Custom token-budget injection.** Server-side feature; auto-applied to API callers.

## 4. Design

### 4.1 The `ProviderCapabilities` struct

A frozen dataclass returned by each provider, computed once at construction.

```python
# plugin_sdk/provider_contract.py

@dataclass(frozen=True, slots=True)
class CacheTokens:
    read: int = 0
    write: int = 0


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    requires_reasoning_resend_in_tool_cycle: bool = False
    reasoning_block_kind: Literal["anthropic_thinking", "openai_reasoning", None] = None
    extracts_cache_tokens: Callable[[Any], CacheTokens] = lambda usage: CacheTokens()
    min_cache_tokens: Callable[[str], int] = lambda model: 0
    supports_long_ttl: bool = False
```

Provider implementations populate these per their own contract:

- **Anthropic**: `requires_reasoning_resend_in_tool_cycle=True`, `reasoning_block_kind="anthropic_thinking"`, extracts `cache_creation_input_tokens` + `cache_read_input_tokens`, model-specific min-cache-tokens (4096 for Opus/Mythos/Haiku 4.5, 2048 for Sonnet 4.6, 1024 for older), `supports_long_ttl=True`.
- **OpenAI** (Chat Completions): `requires_reasoning_resend_in_tool_cycle=False`, extracts `prompt_tokens_details.cached_tokens` as `read` (write=0), `min_cache_tokens=lambda _: 1024`.
- **OpenRouter**: Pass-through. Reads whichever field is present (Anthropic-shape if upstream is Anthropic, OpenAI-shape otherwise). `requires_reasoning_resend_in_tool_cycle=False` (OpenRouter caller does not currently route Anthropic models with extended thinking; if it does in future, add a model-aware branch).
- **All other providers**: defaults — every flag False / None.

### 4.2 Canonical `Usage` extension

Add two fields with default 0:

```python
# plugin_sdk/provider_contract.py
@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0    # NEW
    cache_write_tokens: int = 0   # NEW (only Anthropic-style write tracking; OpenAI leaves 0)
```

### 4.3 Canonical `Message` extension

Add `reasoning_signature: dict | None = None`. Anthropic populates with `{"signature": <opaque_str>}` extracted from the API response's thinking block. Other providers leave None. SessionDB JSON columns ignore unknown keys; old rows deserialize with the default.

### 4.4 Anthropic thinking-block reconstruction

`_to_anthropic_messages()` ([extensions/anthropic-provider/provider.py:250-301](../../../extensions/anthropic-provider/provider.py)) gets one new branch:

> When emitting an assistant message that contains at least one `tool_use` block AND `Message.reasoning` is populated AND `Message.reasoning_signature` is populated AND the provider's `requires_reasoning_resend_in_tool_cycle` is True, prepend a `thinking` block with `{"type": "thinking", "thinking": <text>, "signature": <sig>}` before the `tool_use` block.

The Messages API auto-strips thinking blocks from prior turns when the conversation has moved past the tool cycle, so we do not need to track "is this still in cycle?" ourselves. We just always send when we have signature + tool_use.

### 4.5 `prompt_caching.py` capability awareness

Two small additions to [opencomputer/agent/prompt_caching.py](../../../opencomputer/agent/prompt_caching.py):

1. **Size-threshold filter.** Before placing a marker on a candidate block, estimate its tokens (≈ `len(text) // 4` is good enough; we are filtering, not billing). If under `provider.capabilities.min_cache_tokens(model)`, skip and walk back one position. Up to 4 attempts (the breakpoint budget). If no eligible block found in the window, the request proceeds with fewer or zero markers — same as today, just smarter.
2. **Idle-aware TTL.** When `provider.capabilities.supports_long_ttl` is True AND the gap between the last assistant turn and the current request exceeds 4 minutes, pass `cache_ttl="1h"` to `_apply_cache_marker()`. Otherwise the existing `5m` default. The 4-minute threshold leaves a one-minute safety buffer below the 5-minute cache expiry.

### 4.6 Telemetry surface

`StepOutcome` ([opencomputer/agent/loop.py:2822-2823](../../../opencomputer/agent/loop.py)) gets `cache_read_tokens` and `cache_write_tokens`, defaulting to 0. SessionDB persists. The `/usage` command's rendering layer adds a conditional line:

```
Cache: 12,400 read / 880 written (≈ saved $0.06)
```

The dollar estimate uses the existing model pricing table. Line is suppressed when both numbers are 0 (preserves current display for non-caching providers and old sessions).

## 5. Migration & backwards compatibility

- All new fields default to safe values. Old SessionDB rows deserialize without migration.
- All new behavior is gated behind capability flags; providers that don't opt in see no change.
- Existing tests pass with conservative defaults.
- The thinking-block fix is opt-in per Message: only fires when `reasoning_signature` is populated. Sessions resumed from before this PR (no stored signatures) gracefully skip the fix on their next turn — they get one turn of the old behavior, then the response populates signature for subsequent turns.

## 6. Testing

| # | Layer | What it proves |
|---|---|---|
| 1 | Unit (per provider) | `ProviderCapabilities` defaults snapshot — catches forgot-to-set regressions. |
| 2 | Provider unit (Anthropic) | `_to_anthropic_messages()` emits `thinking` block before `tool_use` when signature present. |
| 3 | Provider unit (Anthropic) | No `thinking` block emitted when message has reasoning but no `tool_use`. |
| 4 | Provider unit (each) | Cache-token extraction maps the provider's wire shape into canonical `Usage`. |
| 5 | Loop unit | Sub-threshold blocks skipped; walks back; succeeds with fewer markers when window has no eligible blocks. |
| 6 | Loop unit (faked clock) | Idle gap > 4min on `supports_long_ttl=True` provider passes `cache_ttl="1h"`; ≤ 4min passes default; unsupported provider always default. |
| 7 | CLI integration | `/usage` displays cache line when present, suppressed when both fields zero. |
| 8 | Live smoke (env-gated) | Real Anthropic call with extended thinking + tool use confirms no API error and continued reasoning. Real OpenAI call proves `cache_read_tokens > 0` on second identical request. |

No mocking the provider SDKs. Tests construct synthetic SDK response shapes and feed them through the parser. Live tests gated by env var, off in CI.

## 7. Files touched (additive only — no removals)

- `plugin_sdk/provider_contract.py` — `Usage` extension, `ProviderCapabilities` + `CacheTokens` types.
- `plugin_sdk/core.py` — `Message.reasoning_signature` field.
- `extensions/anthropic-provider/provider.py` — capability declaration, signature extraction in `_parse_response`, thinking-block reconstruction in `_to_anthropic_messages`.
- `extensions/openai-provider/provider.py` — capability declaration, `cached_tokens` extraction.
- `extensions/openrouter-provider/provider.py` (if present) — capability declaration, pass-through cache extraction.
- `opencomputer/agent/prompt_caching.py` — capability-aware threshold filter and TTL switch.
- `opencomputer/agent/loop.py` — `StepOutcome` extension, idle-gap measurement, telemetry plumbing.
- CLI rendering layer for `/usage` — conditional cache line.
- New test files: `tests/test_provider_capabilities.py`, `tests/test_anthropic_thinking_resend.py`, `tests/test_prompt_caching_thresholds.py`, `tests/test_idle_ttl_switch.py`, additions to existing provider tests for cache-token extraction.

## 8. Out-of-scope follow-ups (logged, not built)

- Server-side compaction with `pause_after_compaction` + Layer-3 splice.
- Pre-warming on `oc` boot.
- OpenAI Responses API support for o-series reasoning items.
- Native Gemini provider with `cached_content_token_count`.
- Cost-rate alerts in `/usage` ("your cache hit rate is 3%").

## 9. Parallel-session safety

A second Claude Code session is active in this workspace at the time of writing. Implementation work for this design will:

- Branch off `main`, not the current `spec/tool-use-contract-tightening`.
- Use a git worktree so working directories do not collide.
- Touch only the files listed in §7. Anything else (notably `OpenComputer/opencomputer/auth/`) is out of bounds.
