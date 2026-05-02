# Context-Management Hygiene (Subsystem D)

**Date:** 2026-05-02
**Scope:** Provider-agnostic token-counting capability — accurate input-token counts for any provider, used by `CompactionEngine` and other context-management call sites.
**Status:** Implementing in `feat/context-management-hygiene` (stacked on Subsystems A + B + C).

---

## 1. Problem

Several call sites need an accurate input-token count for messages:

- `CompactionEngine.should_compact(last_input_tokens)` — currently relies on the previous-call's reported tokens. First-call (or out-of-band) compaction triggers can't know.
- Cost guard / budget tracking — needs pre-flight estimate.
- Future Context Editing (Subsystem D follow-up) — needs accurate counts to decide which tool results to drop.

There's no provider-agnostic way to count tokens today. Each provider has its own native method:

- Anthropic: `client.messages.count_tokens(...)` endpoint (server-side, accurate)
- OpenAI: `tiktoken` library (client-side, accurate, supports gpt-* tokenizers)
- Llama/Ollama: their own tokenizers
- Heuristic fallback: ~4 chars per token (rough but provider-agnostic)

## 2. Goals & non-goals

**Goals:**

- Add `count_tokens()` method to `BaseProvider` with a generic heuristic default.
- Anthropic provider overrides with native endpoint.
- OpenAI provider overrides with tiktoken (when available).
- Generic — every provider can implement; default is reasonable for any.
- Tests covering default + 2 native overrides.

**Non-goals (deferred):**

- Wiring `count_tokens` into `CompactionEngine` — separate decision per call site.
- Native implementations for Llama/Ollama/Kimi providers — they ship when those providers add the capability.
- Tool-result trap audit — empty-`end_turn` retry from Subsystem A already recovers; auditing for prevention is low value relative to other work.
- Server-side context editing (Anthropic-only feature) — different concern from token counting.

## 3. Approach

### 3.1 Provider contract extension

Add to `BaseProvider` in `plugin_sdk/provider_contract.py`:

```python
async def count_tokens(
    self,
    *,
    model: str,
    messages: list[Message],
    system: str = "",
    tools: list[ToolSchema] | None = None,
) -> int:
    """Count input tokens for these messages.
    
    Default implementation uses a heuristic (~4 chars per token).
    Providers should override for accurate counts.
    """
    # Default: heuristic
    return _heuristic_token_count(messages, system)
```

Add a private helper:

```python
def _heuristic_token_count(messages: list[Message], system: str) -> int:
    """~4 chars per token. Provider-agnostic, lower-bound estimate."""
    total = len(system)
    for m in messages:
        total += len(m.content or "")
        for tc in (m.tool_calls or []):
            total += len(tc.name) + len(json.dumps(tc.arguments))
    return total // 4
```

### 3.2 Anthropic provider implementation

```python
async def count_tokens(self, *, model, messages, system="", tools=None) -> int:
    response = await self.client.messages.count_tokens(
        model=model,
        messages=self._to_anthropic_messages(messages),
        system=system if system else None,
        tools=[t.to_anthropic_format() for t in (tools or [])],
    )
    return response.input_tokens
```

### 3.3 OpenAI provider implementation

```python
async def count_tokens(self, *, model, messages, system="", tools=None) -> int:
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model(model)
    except (ImportError, KeyError):
        return _heuristic_token_count(messages, system)
    
    total = len(enc.encode(system))
    for m in messages:
        total += len(enc.encode(m.content or ""))
        for tc in (m.tool_calls or []):
            total += len(enc.encode(tc.name + json.dumps(tc.arguments)))
    return total
```

### 3.4 Generic-by-design checklist

- ✅ Method on `BaseProvider` — every provider implements
- ✅ Default heuristic — works for any provider that doesn't override
- ✅ Native overrides per provider — accuracy where it matters
- ✅ Future Kimi/DeepSeek/Llama/Ollama providers add their tokenizer overrides without core changes

## 4. Components & file map

| File | Change |
|---|---|
| `plugin_sdk/provider_contract.py` | NEW: `_heuristic_token_count` helper. MODIFY: `BaseProvider.count_tokens` method (concrete default — not abstract). |
| `extensions/anthropic-provider/provider.py` | NEW: `count_tokens` override using native endpoint. |
| `extensions/openai-provider/provider.py` | NEW: `count_tokens` override using tiktoken. |
| `tests/test_count_tokens.py` | NEW: default heuristic + Anthropic + OpenAI overrides. |

## 5. Acceptance criteria

1. Full pytest suite green.
2. Ruff clean.
3. New tests cover heuristic fallback, Anthropic override, OpenAI override.
4. BC preserved — existing providers without override get heuristic default automatically.

## 6. Out of scope (follow-up PRs)

- Wire `count_tokens` into CompactionEngine
- Native Llama/Ollama/Kimi tokenizer overrides
- Cost-guard pre-flight estimation
- Server-side context editing
