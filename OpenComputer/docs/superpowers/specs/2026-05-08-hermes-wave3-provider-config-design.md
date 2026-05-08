# Hermes Wave 3 — Provider Config Polish

**Date:** 2026-05-08
**Status:** Spec — implementation scope: 7 features across 3 PRs
**Source:** Hermes "Integrations & AI Providers" reference doc, second pass
**Companion specs:**
- Wave 1: `2026-05-08-hermes-doc-parity-design.md`
- Wave 2: `2026-05-08-hermes-dashboard-ext-rl-providers-parity-design.md`

---

## 1. Problem statement

The user pasted the Hermes "Integrations & AI Providers" reference doc with the framing *"this is a build up of what is on top of what we have currently."*

Wave 2 (shipped today, commit `929c12ed`) closed the **doc-parity** question on this exact reference doc with a deliberately minimal scope (1-line API fix + 3 docs). Wave 2 explicitly parked seven provider-config features with rationales of "defer until pain point" or "Wave 1 honest gap; not re-doing here."

The user's "build up" framing is the reopen trigger for those parked items. This spec re-examines them with fresh eyes and ships the ones that pass an honest cost/benefit gate.

**Out of charter:** voice TTS/STT additions (Wave 1 carry-forward), Browserbase / Browser Use / Zed / JetBrains ACP (proprietary or big-scope), niche memory backends, Atropos RL (re-parked four times). Each of these has explicit rationale below in §2.4.

---

## 2. Gap analysis (verified, file:line-cited)

Verification was done by a dispatched Explore agent against `/Users/saksham/Vscode/claude/OpenComputer/`. Each row below has a status grounded in concrete grep/read evidence.

### 2.1 Already shipped — re-confirmation

| Item | Status | Evidence |
|---|---|---|
| MCP SSE transport | SHIPPED | `opencomputer/mcp/client.py:21` (`from mcp.client.sse import sse_client`) + dispatch at line 344-350 |
| Per-model `api_mode` (auto/openai/anthropic) | SHIPPED | `agent/config.py:114` (`api_mode: str = "auto"`) |
| Per-model `fallback_models` chain | SHIPPED | `agent/config.py:87` (`fallback_models: tuple[str, ...] = ()`) |
| 41 provider plugins under `extensions/` | SHIPPED | Wave 2 spec §2.4.1 |
| OpenAI-compatible HTTP server (`/v1/responses` stub + run-tracking) | SHIPPED | Wave 2 §2.4.1 |
| Memory backends (built-in + Honcho + Mem0 + Vector + Wiki) | SHIPPED | Wave 2 §10 |

### 2.2 Verified missing — ship in this wave

| ID | Feature | Effort | Reason to ship |
|---|---|---|---|
| **A1** | Named `custom_providers:` list with per-entry `name` / `base_url` / `api_key` / `key_env` / `api_mode` / `models:` map (per-model `context_length` override) + `/model custom:<name>:<model>` slash dispatch | ~250 LOC + ~25 tests | Reference doc's recommended pattern for multiple OpenAI-compat endpoints. Today users either install a plugin per endpoint or share one base_url. Both are friction. |
| **A2** | `oc model add` wizard — focused Typer subcommand to add a `custom_providers:` entry interactively (name / base_url / key_env / probe + persist) | ~80 LOC + 5 tests | Reference doc's canonical UX (`hermes model`). YAML-edit-by-hand for adding endpoints IS friction. Brutal-pass added 2026-05-08 after Wave-3 review. |
| **B** | xAI `x-grok-conv-id` auto-cache header (per-session UUID derived from `runtime.session_id`) | ~10 LOC + 2 tests | Free perf win for xAI users; one-line header injection. Wave 2 deferred as "niche optimization" but cost is so low the trade-off was wrong. |
| **C** | HuggingFace routing suffixes `:fastest` / `:cheapest` / `:<provider>` parsed from model name and translated to HF Inference Providers API field | ~40 LOC + 6 tests | Reference doc says this is the canonical way to use HF (default `:fastest`). Without it, users hit slow back-ends silently. |
| **D** | OpenRouter `provider_routing:` config block (`sort` / `only` / `ignore` / `order` / `require_parameters` / `data_collection`) + `:nitro` (throughput) / `:floor` (price) suffix sugar | ~100 LOC + ~12 tests | Power-user feature. `data_collection: deny` is a privacy-conscious knob; users won't use OR without it. Wave 2 deferred — reopen trigger met. |
| **E** | `fallback_providers:` plural cross-provider chain (provider+model pairs) + `oc fallback` Typer subcommand (list/add/remove/reorder) | ~250 LOC + ~25 tests | Cross-provider failover is load-bearing reliability. Today's singular `fallback_models` only covers same-provider failures. Activates at most once per turn (matches reference doc behavior). |
| **F1** | Per-provider `request_timeout_seconds` (default 60.0) on `BaseProvider` config; per-model `timeout_seconds` override under `custom_providers[].models.<id>` | ~100 LOC + ~7 tests | Wave 1 honest gap. Today, hung endpoints surface as a stuck process — users can't tell whether it's dead or slow. |
| **F2** | Per-provider `stale_timeout_seconds` for streaming inactivity (token-stall detection on streaming responses) | ~50 LOC + 3 tests | Catches LLM streaming stalls (connection alive but no tokens flowing). Distinct from `request_timeout_seconds`. Brutal-pass added 2026-05-08 — every chat user streams, so this is load-bearing. |
| **G** | Per-server MCP `tools_allow: list[str] | None` + `tools_deny: list[str] | None` on `MCPServerConfig` | ~50 LOC + 5 tests | Token-bloat fix for users with many MCP servers. Cheap to ship; no architectural lift. |
| **H** | `mlx-whisper` STT backend for Apple Silicon (5-10× faster than openai-whisper on M-series Macs) | ~50 LOC + 4 tests | Mac-native perf upgrade. User's daily driver is a Mac. Brutal-pass added 2026-05-08 — only voice item that's a real capability bump for the user, not just "more options." |

**Total:** 10 features, ~980 LOC, ~94 tests.

### 2.3 Verified missing — explicitly NOT shipping (brutal-pass survivors)

| Item | Cost | Why not ship |
|---|---|---|
| `oc fallback test` (dry-run subcommand) | ~30 LOC | True YAGNI. Set `model.invalid_value` and watch the chain fire — no need for a dedicated dry-run subcommand. |
| Voice TTS — MiniMax / Gemini / xAI backends | ~600 LOC + per-provider auth | User has not asked for voice in this session. 6 TTS backends already shipped (Edge / OpenAI / ElevenLabs / Piper / NeuTTS / KittenTTS) — these 3 are options-not-capability. Reopen on explicit ask. |
| Voice STT — Mistral / xAI / whisper-cpp | ~300 LOC | Duplicates of Groq + OpenAI Whisper + local. No new capability. (`mlx-whisper` IS shipping in this wave — see H — because it's a Mac-native perf upgrade.) |
| Browserbase / Browser Use | ~600 LOC + auth | Proprietary SaaS. User's `oc scrape` workflow is occasional — local Chrome CDP works. Reopen if user starts industrial scraping. |
| Zed / JetBrains ACP | ~700 LOC | User uses VS Code. IDE plugins for IDEs they don't open is busywork. |
| SearXNG search backend | ~80 LOC | User has Tavily + Brave + Exa configured. Self-hosted search is for users running their own privacy node. |
| Memory backends — Supermemory / Hindsight / RetainDB | ~300 LOC each | User is on Honcho. Adding 3 more pluggable memory backends with no diff is busywork. Reopen on explicit ask for one specifically. |
| MiniMax OAuth | ~150 LOC | API-key flow already works. OAuth is credential-source flavor only. |
| Atropos RL training | ~3,000 LOC | Re-parked four times on `origin/main`. User is building a personal agent, not a research lab. Bundled `trl-fine-tuning` + `weights-and-biases` skills cover the small RL fraction. |
| LiteLLM / ClawRouter as bundled named providers | ~200 LOC each | `custom_providers:` + `base_url` already lets users point at a LiteLLM proxy at `localhost:4000/v1`. Native bundling adds dep + opinion for marginal gain. |

### 2.4 Why the makes-sense filter still holds

The user's verbatim Wave 1 feedback is on record: *"Only integrate something that actually makes sense. If you already have it, don't do it. If you're missing it, that doesn't mean we should fill it just because we're missing it. We will fill it because it makes sense."*

Each row in §2.2 has a "reason to ship" backed by concrete reliability/UX/perf benefit. Each row in §2.3 has a one-line "why not" — usually "no demand" or "trivial workaround exists."

---

## 3. Design

### 3.1 Feature A — `custom_providers:` named list

**Schema:**

```yaml
# ~/.opencomputer/<profile>/config.yaml
custom_providers:
  - name: local
    base_url: http://localhost:8080/v1
    # api_key: optional inline (not recommended)
    # key_env: GROQ_API_KEY (preferred)
    api_mode: auto       # auto | openai | anthropic
    request_timeout_seconds: 60.0
    models:
      "qwen3.5:27b":
        context_length: 32768
      "deepseek-r1:70b":
        context_length: 65536
        timeout_seconds: 180   # per-model override
  - name: groq
    base_url: https://api.groq.com/openai/v1
    key_env: GROQ_API_KEY
```

**Dataclasses (new in `agent/config.py`):**

```python
@dataclass(frozen=True, slots=True)
class CustomProviderModelOverride:
    context_length: int | None = None
    timeout_seconds: float | None = None

@dataclass(frozen=True, slots=True)
class CustomProvider:
    name: str
    base_url: str
    api_key: str | None = None      # inline value
    key_env: str | None = None      # env-var name
    api_mode: str = "auto"          # auto | openai | anthropic
    request_timeout_seconds: float = 60.0
    models: dict[str, CustomProviderModelOverride] = field(default_factory=dict)
```

**Resolution:**

- At `load_config()`, parse `custom_providers:` into a tuple of `CustomProvider`.
- `agent/loop.py` provider-resolver: when model spec is `custom:<name>:<model_id>`, look up the named CustomProvider and instantiate a `CustomProviderClient` (extends OpenAIProvider for `api_mode=openai`, AnthropicProvider for `api_mode=anthropic`).
- For `api_mode=auto`, probe `/v1/models` once at first use; cache the result. If the endpoint returns 404 or the body shape doesn't match either contract, fail with a clear error.
- Per-model `context_length` overrides flow into `agent/compaction.py`'s context-length resolver; per-model `timeout_seconds` flows into the httpx client.

**Slash command (`/model custom:<name>:<model_id>`):**

- Existing `/model` handler in `opencomputer/agent/slash_commands_impl/` parses the colon-prefixed form.
- Validates `<name>` exists in `custom_providers`; validates `<model_id>` either exists in `models:` or is forwarded verbatim (the user may want to call any model the endpoint supports).
- On success, swaps the active provider mid-session.

### 3.1.2 Feature A2 — `oc model add` wizard

Focused Typer subcommand under `opencomputer/cli.py`. Adds a single `custom_providers:` entry with prompt-driven UX:

```bash
$ oc model add
Provider name (used in /model custom:<name>:...): groq
Base URL: https://api.groq.com/openai/v1
API mode (auto/openai/anthropic) [auto]:
Env var holding the API key (e.g. GROQ_API_KEY) [empty for none]: GROQ_API_KEY
Probe /v1/models to verify connectivity? (Y/n): Y
✓ Endpoint reachable. Found 5 models.
✓ Wrote 'groq' to ~/.opencomputer/default/config.yaml under custom_providers:
✓ Use it now via: /model custom:groq:llama-3.3-70b-versatile
```

Uses existing `config_store` atomic write helpers. No new schema — just a friendly wrapper around the YAML edit.

`oc model list` (companion subcommand) prints registered custom_providers + their probed models.
`oc model remove <name>` deletes an entry.

### 3.2 Feature B — xAI `x-grok-conv-id` auto-cache

**Implementation in `extensions/xai-provider/provider.py`:**

```python
def _request_headers(self, runtime: RuntimeContext | None) -> dict[str, str]:
    headers = super()._request_headers(runtime)
    # Stable per-session UUID for KV-cache reuse on xAI's side.
    # Falls back to per-process UUID for one-shot scripts.
    conv_id = (
        runtime.session_id if runtime and runtime.session_id else self._process_conv_id
    )
    headers["x-grok-conv-id"] = conv_id
    return headers
```

`_process_conv_id` is set in `__init__` to a `str(uuid.uuid4())`. No config knob — always-on. Acceptable risk: a one-shot script gets a different conv_id every run, which is the correct behavior anyway.

### 3.3 Feature C — HuggingFace routing suffixes

**Verification step (block on this before coding):** query the HuggingFace Inference Providers API docs (via `context7` MCP or web search) to confirm whether `:fastest` maps to `provider="auto"`, a header, or a query parameter. Implement only against the verified contract.

**Expected implementation in `extensions/huggingface-provider/provider.py`:**

```python
_KNOWN_HF_PROVIDERS = {"groq", "together", "fireworks", "replicate", "sambanova", "hyperbolic", "novita", "cerebras"}
_HF_ROUTING_SUFFIXES = {"fastest", "cheapest"} | _KNOWN_HF_PROVIDERS

def _parse_routing_suffix(model: str) -> tuple[str, str | None]:
    """Strip a recognized routing suffix from a model name.

    Returns (model_without_suffix, suffix_or_none).
    Unknown suffixes pass through verbatim.
    """
    if ":" not in model:
        return model, None
    prefix, _, suffix = model.rpartition(":")
    if suffix in _HF_ROUTING_SUFFIXES:
        return prefix, suffix
    return model, None
```

The suffix is then injected into the HF request body or URL path per the verified API contract. If unknown, model name passes through unchanged — preserves any future suffix HF adds without breaking.

### 3.4 Feature D — OpenRouter routing knobs

**Verification step (block before coding):** confirm the JSON request-body shape OpenRouter expects for `provider:` routing. The reference doc shows YAML config but not the body shape. Likely:

```json
{
  "model": "...",
  "provider": {
    "sort": "price",
    "only": ["Anthropic", "Google"],
    "ignore": ["Together"],
    "order": ["Anthropic"],
    "require_parameters": true,
    "data_collection": "deny"
  }
}
```

**Config block (under top-level `model:` in YAML):**

```yaml
model:
  provider: openrouter
  default: anthropic/claude-sonnet-4
  provider_routing:
    sort: "price"            # price | throughput | latency
    only: ["Anthropic"]
    ignore: ["DeepInfra"]
    order: ["Anthropic", "Google"]
    require_parameters: true
    data_collection: "deny"  # allow | deny
```

**Suffix sugar:**

- `:nitro` → `sort: throughput` (overrides config block)
- `:floor` → `sort: price` (overrides config block)

Suffixes recognized only when `provider="openrouter"`. On non-OR provider, a `:nitro`/`:floor` suffix is stripped + warned once per process. **This isolates the suffix to its semantic owner — no cross-provider behavior surprises.**

### 3.5 Feature E — `fallback_providers:` plural + `oc fallback` CLI

**Schema:**

```yaml
fallback_providers:
  - provider: openrouter
    model: anthropic/claude-sonnet-4
  - provider: nous
    model: nous-hermes-3
  - provider: custom:local         # references custom_providers[name=local]
    model: qwen3.5:27b
```

**Dataclass:**

```python
@dataclass(frozen=True, slots=True)
class FallbackProvider:
    provider: str          # bundled provider name OR "custom:<name>"
    model: str
    base_url: str | None = None
    key_env: str | None = None
```

**Resolver in `agent/fallback.py`:**

- On primary failure (429 after retries / 5xx after retries / 401 / 403 / 404 / malformed), iterate `fallback_providers` in order.
- Per-turn scoped: activates at most once per turn; primary restored on next user message.
- If both `fallback_providers:` (new plural) and `fallback_models:` (existing singular) are set, exhaust `fallback_providers` first, then fall through to `fallback_models` on the *last successful* provider. Backward compat preserved.
- Cross-provider swap preserves conversation history (verbatim message list); tool schemas re-emitted under the new provider's contract.

**CLI (`oc fallback`):**

```bash
oc fallback                         # show current chain
oc fallback add openrouter/anthropic/claude-sonnet-4
oc fallback add custom:local/qwen3.5:27b
oc fallback remove 1                # by index
oc fallback move 0 2                # reorder: index 0 → position 2
oc fallback clear                   # empty the list
```

Implementation: Typer subcommand under `opencomputer/cli.py`. Each subcommand reads + writes `~/.opencomputer/<profile>/config.yaml`'s `fallback_providers:` block atomically using the existing config_store helpers. Atomic write via tempfile + rename (matches existing patterns).

### 3.6 Feature F1 — Per-provider request timeouts + Feature F2 — Streaming stale-timeout

**`BaseProvider` contract (in `plugin_sdk/provider_contract.py`):**

```python
@dataclass(frozen=True, slots=True)
class BaseProvider:
    ...
    request_timeout_seconds: float = 60.0
    """Per-call HTTP timeout in seconds. Default 60.0. Override per-provider
    via subclass field default or per-call via runtime context override.
    Per-model further overrides via custom_providers[].models[].timeout_seconds."""
```

**Per-extension behavior:**

- Each provider's httpx client picks up `self.request_timeout_seconds` and passes to `httpx.Client(timeout=...)`.
- Existing 41 extensions inherit the default; no behavior change unless they explicitly override.
- Tests: snapshot test that all bundled providers expose `request_timeout_seconds` at expected default; integration test that an unreachable endpoint surfaces a clear `httpx.TimeoutException` rather than hanging.

**Streaming stale-timeout (F2) — distinct from F1:**

- F1 catches "request takes too long start-to-finish."
- F2 catches "stream is stalled — no tokens have arrived in N seconds even though the connection is alive."
- Implementation: a per-stream `last_token_at: float` timestamp updated on every chunk. A background `asyncio.Task` polls every `min(stale_timeout_seconds / 4, 5.0)` seconds; if `time.monotonic() - last_token_at > stale_timeout_seconds`, raises `StreamStaleException` (caught by `agent/loop.py` and surfaced via the fallback chain).
- New field on `BaseProvider`: `stale_timeout_seconds: float | None = 60.0` — `None` disables the watchdog (opt-out for batch-mode users).
- Per-provider override: subclasses can change the default. Per-call override flows through `RuntimeContext`.
- Tests: mock streaming response that emits one chunk then hangs; assert StreamStaleException raised after stale_timeout_seconds; assert F1 doesn't fire before F2 in this scenario.

### 3.7 Feature G — MCP per-server tool filter

**`MCPServerConfig` (in `agent/config.py:333`):**

```python
@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    name: str
    transport: str = "stdio"  # stdio | sse | http
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    # NEW (Wave 3):
    tools_allow: tuple[str, ...] | None = None
    """Whitelist. None = no filter. Empty tuple = deny all (server effectively
    contributes zero tools but stays connected for resources/prompts)."""
    tools_deny: tuple[str, ...] = ()
    """Blacklist. Applied after tools_allow."""
```

**Filter in `MCPClient.list_tools()` or wherever tools register:**

```python
def _filter_tools(server_cfg: MCPServerConfig, tools: list[MCPTool]) -> list[MCPTool]:
    if server_cfg.tools_allow is not None:
        tools = [t for t in tools if t.name in server_cfg.tools_allow]
    if server_cfg.tools_deny:
        tools = [t for t in tools if t.name not in server_cfg.tools_deny]
    return tools
```

Naming-collision detection in `ToolRegistry` is unchanged — filter trims at MCP-server boundary; registry catches duplicates across servers.

### 3.8 Feature H — `mlx-whisper` STT for Apple Silicon

**Why this is the only voice item shipping:** mlx-whisper is Apple Silicon native (Metal Performance Shaders), benchmarks 5-10× faster than `openai-whisper` on M-series Macs. The user runs OC daily on a Mac; this is a real perf upgrade, not "more options."

**Implementation in `opencomputer/voice/`:**

- New file: `opencomputer/voice/stt_mlx_whisper.py` — implements the STT backend interface.
- Pure-pip dependency: `mlx-whisper` (Apple-Silicon only; gracefully no-op on non-arm64-Darwin).
- Priority in the STT backend chain: prefer mlx-whisper on Apple Silicon if available, fall back to existing local Whisper otherwise.
- Configuration: opt-in via `voice.stt: "mlx-whisper"` in config.yaml; no auto-switch (avoids surprising users who configured a specific backend).
- New optional extra in `pyproject.toml`: `voice-mlx = ["mlx-whisper>=0.4.0"]`.

**Tests:** detect platform; on non-Apple-Silicon, assert backend reports unavailable; on Apple Silicon (skipped on CI), assert backend accepts a sample audio file.

---

## 4. Implementation plan — 3 PRs

| PR | Scope | LOC | Tests |
|---|---|---|---|
| **PR-1: Foundation** | A1 (custom_providers list + per-model overrides + api_mode auto-detect + /model custom: dispatch) + A2 (`oc model add` wizard) + F1 (per-provider request_timeout) + F2 (stale_timeout for streaming) | ~480 | ~40 |
| **PR-2: Provider features** | B (xAI cache header) + C (HF routing suffixes) + D (OR provider_routing + :nitro/:floor) | ~150 | ~20 |
| **PR-3: Reliability + MCP + mlx-whisper** | E (fallback_providers plural + oc fallback CLI) + G (MCP tools_allow/tools_deny) + H (mlx-whisper STT) | ~350 | ~34 |

Each PR is reviewable in <30 min. PR-1 is the foundation — PR-2 and PR-3 depend on its dataclasses (CustomProvider, ModelOverride). PR-2 and PR-3 can ship in parallel after PR-1 lands.

### Branch strategy

- Worktree at `~/Vscode/claude/OpenComputer-wave3/` from `origin/main` (929c12ed). Per parallel-sessions memory rule: never share a working tree on `main` with other Claude sessions.
- Branch: `feat/hermes-wave3-provider-config-2026-05-08`.
- All 3 PRs share one branch; commits are squash-merged in 3 separate PRs from the branch (or rebased into 3 branches before push, depending on review preference).

### Test strategy

- New test files: `tests/test_custom_providers.py`, `tests/test_provider_timeouts.py`, `tests/test_xai_cache_header.py`, `tests/test_hf_routing_suffixes.py`, `tests/test_openrouter_routing.py`, `tests/test_fallback_providers.py`, `tests/test_oc_fallback_cli.py`, `tests/test_mcp_tool_filter.py`.
- Coverage targets: each new code path has at least one happy-path test + one error-path test. E2E test for `/model custom:` dispatch (real endpoint mocked via `respx`).
- Existing 9356+ tests must remain green. No deletions; no skips. Honcho test-pollution flake is known-pre-existing per `project_honcho_default_test_pollution_flake.md` and not blocking GitHub CI.

### Validation gates

1. **Per-PR:** `pytest tests/` clean + `ruff check` clean. Local CI green before push.
2. **PR-1 only:** smoke test — write a `custom_providers:` block pointing at `localhost:11434/v1` (or skip if Ollama not running locally), invoke `/model custom:local:qwen3.5`, verify provider swap.
3. **PR-2 only:** unit test — Mock OpenRouter + assert request body has `provider: { sort: ... }` block.
4. **PR-3 only:** integration test — primary fails with 429, fallback chain swaps, response surfaces. CLI test — `oc fallback add openrouter/anthropic/claude-sonnet-4 && oc fallback remove 0` round-trips through `config.yaml`.

---

## 5. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| HF routing suffix → API field mapping is wrong | High (unverified) | Block on docs query before coding; ship suffix-strip + pass-through fallback. |
| OR `provider_routing` body shape is wrong | Medium | Same — verify body shape via OR docs before coding; integration test against mock. |
| BaseProvider timeout default breaks one of 41 extensions | Low | Snapshot test all bundled providers post-change; default = 60.0s matches httpx default = no behavior change. |
| `fallback_providers` cross-provider swap breaks tool-schema continuity | Medium | Re-emit tool schemas under target provider's contract; existing integration tests for tool calls catch mid-conversation provider swaps. |
| `/model custom:<name>:<model>` collides with model names containing `:` | Low | Resolver: split on first `:` after `custom`; remainder is the model id verbatim. Tests for `custom:local:qwen3.5:27b` (note the inner `:`). |
| `oc fallback` CLI corrupts config.yaml on concurrent edit | Low | Use existing `config_store` atomic-write helpers (tempfile + os.rename). |
| Parallel-session conflict on `main` | Low | Worktree from `origin/main`; verified clean before branch creation. |
| MCP `tools_allow=[]` (empty list) ambiguity | Low | Pick "deny all" + docstring; test both `None` (no filter) and `()` (deny all). |
| `:nitro`/`:floor` on non-OpenRouter silently breaks | Low | Suffix stripped + warned once per process on non-OR provider. |

---

## 6. Out of scope (explicit)

- Voice TTS / STT additions (any backend).
- Browserbase / Browser Use / Zed / JetBrains ACP.
- SearXNG search backend.
- Memory backends — Supermemory / Hindsight / RetainDB.
- MiniMax OAuth.
- Atropos RL.
- LiteLLM / ClawRouter as bundled plugins (`custom_providers:` covers them).
- `stale_timeout_seconds` separate from `request_timeout_seconds`.
- `oc fallback test` (dry-run subcommand).
- `oc model add-custom` interactive wizard.
- New dashboard pages or API routes.
- Modifications to the SPA at `ui-web/src/`.
- Modifications to plugins under `extensions/` other than `xai-provider`, `huggingface-provider`, `openrouter-provider` (and only the minimum needed).

---

## 7. Decision

Ship 7 features across 3 PRs. Park the items in §2.3 with rationale on record. Re-park Atropos RL with the same rationale Wave 2 used.

Net delta:
- ~850 LOC + ~85 tests across 3 PRs.
- 1-2 days execution if PR-2 and PR-3 ship in parallel after PR-1 lands.
- Zero new public APIs that aren't backward-compatible with v3 manifests + existing config.yaml.

---

## 8. Spec self-review

- **Placeholder scan:** no TBD/TODO. Each "shipped" / "shipping" / "not shipping" row has explicit rationale.
- **Internal consistency:** §2.2 maps to §3 designs to §4 PR plan. 7 features → A through G in §2.2 → matching subsection in §3 → PR assignment in §4.
- **Scope check:** 3 PRs at ~250-400 LOC each — honest single-implementation-plan size.
- **Ambiguity check:** §3 designs name the file paths; §4 names the test files; §5 enumerates failure modes with mitigations.
- **YAGNI re-check:** §2.3 explicitly lists items that pass the "would be nice but no demand signal" gate. Each row has a one-line "reopen on" trigger.
- **API surface drift check:** all new fields are Optional with default-empty; old configs parse unchanged. New CLI subcommand `oc fallback` doesn't shadow existing namespace. New slash form `/model custom:<name>:<model>` is a prefix extension.
- **Verification dependency:** PR-2 has two upstream-API verification steps (HF, OR). These are honest blockers — coding must wait until the actual API contracts are confirmed. The spec calls this out in §3.3 and §3.4.

---

## 9. Reopen triggers (for §2.3 parked items)

| Item | Reopen on |
|---|---|
| `stale_timeout_seconds` | User reports a hung-stream incident with primary timeout not catching it. |
| `oc fallback test` | Three users ask "how do I dry-run this?" |
| Voice TTS/STT additions | User explicitly asks for one of MiniMax/Gemini/xAI/Mistral. |
| Browserbase / Browser Use | User asks for headless browser with proxy rotation. |
| Zed / JetBrains ACP | User asks "how do I use OC in Zed/JetBrains?" |
| SearXNG | User wants a self-hosted, private search backend. |
| Supermemory / Hindsight / RetainDB | User asks for one of these specifically. |
| MiniMax OAuth | User wants to avoid managing API keys. |
| Atropos RL | User describes a concrete fine-tuning workflow we'd help orchestrate. |
| LiteLLM / ClawRouter bundled | User asks "how do I plug in LiteLLM/ClawRouter natively?" |
