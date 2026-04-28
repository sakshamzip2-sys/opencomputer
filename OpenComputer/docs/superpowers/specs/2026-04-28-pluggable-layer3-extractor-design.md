# Pluggable Layer 3 Extractor — design spec

**Date:** 2026-04-28
**Status:** Draft → revised after audit (see § 9)
**Goal:** Let users choose the LLM backend Layer 3 deepening uses to extract structured signals from artifacts (files, browser pages, calendar events). Today the only option is Ollama; users who already have an Anthropic / OpenAI key shouldn't have to install a second runtime to get deepening working.

---

## 1. The problem in one paragraph

`opencomputer/profile_bootstrap/llm_extractor.py` calls Ollama via subprocess. If Ollama isn't on PATH, `extract_artifact()` raises `OllamaUnavailableError` and Layer 3 emits zero motifs. Users who already pay for Anthropic/OpenAI shouldn't need a second LLM stack — they're already sending conversation content to the same providers, so the privacy ship hasn't sailed for *them*. But for users who *do* care (the privacy-conscious default audience), Ollama must remain the default and the most obvious option.

## 2. Constraints (load-bearing)

1. **`ArtifactExtraction` shape stays frozen.** Existing consumers in `orchestrator.extract_and_emit_motif` already destructure it. Schema change = breaking change.
2. **Privacy-by-default.** Default extractor is still `ollama`. A switch to API requires explicit user action (config edit) so the privacy posture isn't accidentally weakened.
3. **Cost-bounded.** API extractors must respect a daily cap; a runaway deepening loop on 1000 files at $0.001/call could turn into $1+/day silently. Use existing `cost_guard` infra.
4. **Match existing failure semantic.** Today: missing-Ollama raises; malformed-JSON / timeout returns blank `ArtifactExtraction()`. New backends preserve both behaviors so callers don't change.
5. **No new dependencies for the API path.** Anthropic/OpenAI providers already ship with the agent (`extensions/anthropic-provider/`, `extensions/openai-provider/`). Reuse them.

## 3. Three options considered

**Option A — Inline branching.** Add `if config.extractor == "ollama": ...; elif "anthropic": ...` inside `extract_artifact()`. Cheapest to ship. Worst to maintain — the function grows a tangle of branches and per-backend prompt formatting.

**Option B — Protocol + implementations + factory.** ⭐ Recommended. Define an `ArtifactExtractor` Protocol with `is_available()` + `extract()` methods. Three classes implement it (Ollama / Anthropic / OpenAI). A factory in the same module reads `config.deepening.extractor` and returns the right instance. Mirrors the `Classifier[L]` abstraction we shipped in PR #201 — same shape, same testing posture, zero novel patterns.

**Option C — Plugin-based extractors.** Extractors as full plugins under `extensions/`. Most flexible but overkill — extractors are stateless functions, not channel/provider-grade lifecycle citizens. Skip.

**Picked B.** It scales cleanly to a 4th/5th backend (Gemini, local llama-cpp, etc.) without touching the call site, and matches the shape pattern we already validated.

## 4. Architecture

```
opencomputer/profile_bootstrap/
├── llm_extractor.py        ← module renamed conceptually but file stays
│   ├── ArtifactExtraction   (existing, frozen — no change)
│   ├── ArtifactExtractor    (new Protocol)
│   ├── OllamaArtifactExtractor   (existing logic moved into class)
│   ├── AnthropicArtifactExtractor   (new — uses BaseProvider)
│   ├── OpenAIArtifactExtractor       (new — uses BaseProvider)
│   ├── get_extractor(config) -> ArtifactExtractor   (factory)
│   └── extract_artifact(content) -> ArtifactExtraction
│         (back-compat shim — calls get_extractor().extract())
└── orchestrator.py
    └── extract_and_emit_motif() — unchanged surface, picks backend via config
```

Public contract preserved: existing `extract_artifact()` and `OllamaUnavailableError` keep their names + signatures. Internal callers get the protocol.

## 5. Configuration

New dataclass `DeepeningConfig` lives next to `MemoryConfig` in `agent/config.py`:

```python
@dataclass(frozen=True, slots=True)
class DeepeningConfig:
    """Layer 3 deepening — content extractor + cost controls."""

    extractor: Literal["ollama", "anthropic", "openai"] = "ollama"
    """Which LLM runs the extraction. ``ollama`` is privacy-default
    (content never leaves the machine). ``anthropic`` / ``openai``
    send artifact content to the respective API."""

    model: str = ""
    """Model id passed to the extractor. Empty → backend-specific
    sensible default (``llama3.2:3b`` for ollama,
    ``claude-haiku-4-5-20251001`` for anthropic, ``gpt-4o-mini``
    for openai)."""

    daily_cost_cap_usd: float = 0.50
    """Per-day spend ceiling for API extractors. The cost guard
    skips deepening when reached. Ollama is unaffected (zero cost)."""

    max_artifacts_per_pass: int = 100
    """Hard cap regardless of cost — prevents one pass from
    dominating CPU/network even if cap allows."""

    timeout_seconds: float = 15.0
    """Per-extraction wall-clock timeout. API backends translate
    this into request timeout."""
```

Loaded via the existing `load_config()` path. Top-level YAML key `deepening:`.

## 6. Privacy notice on switch

When the *first* `deepen` call runs after the user changes `extractor` from `ollama` to anything else, print a one-time banner:

```
ⓘ  Layer 3 deepening is now using <backend>.
   Artifact content (file bodies, browser pages) will be sent to
   <provider> for extraction. To stay local, set
   `deepening.extractor: ollama` in config.yaml.
```

Stash a marker file at `~/.opencomputer/<profile>/deepening_consent_<backend>.acknowledged` so the banner only prints once per backend per profile.

## 7. Cost-guard integration

Reuse `opencomputer/cost_guard/guard.py:record_usage`. After each successful API extraction, record `(provider, input_tokens, output_tokens)`. Before each call, check `cost_guard.daily_spend_usd(provider)` against `config.deepening.daily_cost_cap_usd`. If over → raise `DeepeningCostCapExceeded`, orchestrator catches and skips remaining artifacts in this window with a single log line.

Ollama path bypasses cost guard entirely (zero cost).

## 8. Failure semantics matrix

| Failure | Ollama | Anthropic | OpenAI |
|---|---|---|---|
| Backend unavailable (binary missing / API key missing) | `OllamaUnavailableError` | `OllamaUnavailableError`-shaped subclass | same |
| Per-call timeout | blank `ArtifactExtraction()` | blank | blank |
| Malformed JSON in response | blank | blank | blank |
| Non-2xx HTTP / nonzero exit | blank | blank | blank |
| Cost cap exceeded | n/a | `DeepeningCostCapExceeded` | same |

Rename `OllamaUnavailableError` → `ExtractorUnavailableError` and alias the old name for back-compat (`OllamaUnavailableError = ExtractorUnavailableError`). Existing `except OllamaUnavailableError:` catches still match.

## 9. Audit findings (rolled in 2026-04-28)

Stress-tested the design against real-world constraints. Issues found + fixes:

1. **`Literal["ollama", "anthropic", "openai"]` is brittle.** Future backends (Gemini, llama-cpp) require a Literal change which is a breaking config schema change. **Fix:** make it `str` with a runtime check in the factory; document the canonical list. Same approach `model.provider` already uses.

2. **`get_extractor()` shouldn't read config from disk every call.** Deepening passes can run thousands of artifacts; re-loading config that often is wasteful. **Fix:** factory accepts `Config` (already loaded by caller) — orchestrator already has it; no fresh I/O.

3. **API providers need an `httpx`-style retry on 429.** Naively raising on first throttle would leak the cost cap before getting useful work. **Fix:** Anthropic + OpenAI extractors do one exponential-backoff retry, then return blank on second failure. Mirrors the existing provider retry policy in `extensions/anthropic-provider/provider.py`.

4. **Token-cost accounting per-call is provider-specific.** Anthropic returns input/output tokens in the response; OpenAI does too but with different field names. **Fix:** abstract via `ProviderResponse.usage` (already standard in `BaseProvider`). The provider plumbing handles the field name mapping; we just read `usage.input_tokens` + `usage.output_tokens`.

5. **What if both Ollama AND Anthropic are configured but Ollama is on PATH?** Today's behavior would prefer Ollama if `extractor == "ollama"`. **Fix:** the config field is the source of truth, no auto-detection magic. If user sets `extractor: anthropic` but the API key is missing, raise (don't silently fall back) — silent fallback would defeat their explicit choice.

6. **Tests need to NOT hit real APIs.** Stub `BaseProvider.complete()` with a fake provider returning a `ProviderResponse` carrying canned JSON. Existing pattern in `test_memory_dreaming.py` (`AsyncMock(spec=BaseProvider)`) is the template.

7. **Sync vs async surface.** `extract_artifact()` is sync today. Anthropic's `BaseProvider.complete()` is `async`. **Fix:** the factory returns a sync facade; under the hood, async backends use `asyncio.run()` per call. Per-call event loop spin-up is fine because deepening is already a slow background pass — call latency is dominated by the LLM, not the loop overhead. Document that `extract()` blocks; if deepening ever moves to async, add an `aextract()` later.

8. **`_DEFAULT_MODEL = "llama3.2:3b"` is constant inside the module.** That's fine for ollama but the new config wants per-extractor defaults. **Fix:** each extractor class owns its `_DEFAULT_MODEL` as a class attribute; `get_extractor()` reads `config.deepening.model` and falls back to the class default if empty.

## 10. What we are NOT shipping in this PR

- **Streaming extraction.** Sync, single-shot only. Streaming buys nothing for an extraction prompt that returns ~200 tokens of JSON.
- **Embedding-based extraction.** Today's prompt → JSON shape stays. Embeddings are a different layer (similarity search, not field extraction).
- **Per-artifact provider override.** All artifacts in a deepening pass use the same backend. Mixing would complicate cost accounting + privacy posture without a real use case.
- **Auto-fallback Ollama → API.** If Ollama is unavailable AND user picked Ollama, deepening reports zero motifs and a hint to install. We don't silently ship their content to a cloud API just because the local one died.
- **Schema migration of the existing `_DEFAULT_MODEL = "llama3.2:3b"` constant.** Stays as the Ollama-class default to preserve back-compat for any direct importers.

## 11. Done definition

- `extractor: anthropic` in config.yaml + valid `ANTHROPIC_API_KEY` → `oc profile deepen --force` emits N>0 motifs against real artifacts.
- All existing call sites in `orchestrator.py` work without code change beyond the factory.
- `pytest tests/test_profile_bootstrap_extractor*.py -v` → all green; existing extractor tests unmodified except for the protocol-conformance one.
- Cost guard records usage per API call; `oc cost show` reflects deepening spend.
- Existing `OllamaUnavailableError` import paths still work.
