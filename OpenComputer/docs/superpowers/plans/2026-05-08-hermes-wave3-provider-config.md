# Hermes Wave 3 — Provider Config Polish — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the 10 verified provider/integration gaps surfaced by the brutal-pass review of the Hermes "Integrations & AI Providers" reference doc, in 3 sequential PRs.

**Architecture:** Three sequential PRs share one branch. PR-1 lands the foundation (custom_providers + timeouts + wizard); PR-2 ships provider-feature improvements (xAI cache, HF suffixes, OR routing); PR-3 ships reliability + MCP + mlx-whisper. New config is purely additive — every old config.yaml parses unchanged. Worktree isolates work from parallel sessions on `main`.

**Tech Stack:** Python 3.12+, frozen+slots dataclass config, httpx, Typer for CLI, pytest+respx for HTTP mocks.

---

## Pre-flight

### Task 0: Worktree setup

- [ ] Verify clean state: `git -C ~/Vscode/claude/OpenComputer status --short`
- [ ] Confirm origin/main alignment: `git log --oneline origin/main..HEAD` (empty)
- [ ] Create worktree: `git worktree add -b feat/hermes-wave3-provider-config-2026-05-08 ~/Vscode/claude/OpenComputer-wave3 origin/main`
- [ ] Editable install: `cd OpenComputer-wave3 && python3 -m venv .venv && source .venv/bin/activate && pip install -e .`
- [ ] Baseline: `pytest tests/ -x -q 2>&1 | tail -10`

---

## PR-1: Foundation — custom_providers + wizard + timeouts

### Task 1: `CustomProvider` dataclass

**Files:** `opencomputer/agent/config.py`, `tests/test_custom_providers.py`

- [ ] Write failing test for `CustomProvider(name, base_url, ...)`, `CustomProviderModelOverride(context_length, timeout_seconds)`, and `__post_init__` validation (api_mode in `_VALID_API_MODES`, request_timeout_seconds > 0).
- [ ] Implement the two dataclasses in `config.py` (frozen+slots). `models: dict` field with `compare=False, hash=False` (matches existing pattern at config.py:97).
- [ ] Add `custom_providers: tuple[CustomProvider, ...] = ()` to top-level Config.
- [ ] Wire YAML parsing in `config_store.py` via `_parse_custom_providers(raw)` helper.
- [ ] Roundtrip test: write YAML, load, assert all fields present.
- [ ] Commit: `feat(wave3): CustomProvider dataclass + config-schema parsing`.

### Task 2: `build_custom_provider` factory + api_mode auto-detect

**Files:** `opencomputer/agent/custom_provider_client.py`, `tests/test_custom_provider_client.py`

- [ ] Failing tests: openai-mode returns OpenAIProvider, anthropic-mode returns AnthropicProvider, auto-mode probes `/v1/models` (with respx mock returning `{"data": [...]}` → openai), key_env resolution from environ, missing key_env logs warning + does not raise.
- [ ] Implement `_resolve_api_key()` (api_key wins, then key_env, then None+warn).
- [ ] Implement `_probe_api_mode(base_url, timeout)` — GET `/v1/models`, check shape, default `"openai"` on error.
- [ ] Implement `build_custom_provider(cp)` — branches on api_mode, instantiates extension provider class with `request_timeout_seconds`.
- [ ] Resolve actual import paths (`extensions.anthropic_provider.provider` etc.) by reading `extensions/anthropic-provider/__init__.py` first.
- [ ] Commit: `feat(wave3): build_custom_provider factory with api_mode auto-detect`.

### Task 3: `/model custom:<name>:<model>` slash dispatch

**Files:** `opencomputer/agent/custom_provider_client.py`, slash handler file (locate via grep), `tests/test_slash_model_custom.py`

- [ ] Failing tests for `parse_custom_model_spec("custom:local:qwen3.5:27b")` → `("local", "qwen3.5:27b")`, simple case `("custom:groq:llama")` → `("groq", "llama")`, error on `"not-custom:..."`.
- [ ] Add parser to `custom_provider_client.py` — `partition(":")` after stripping `custom:` prefix, so model_id keeps inner colons.
- [ ] Locate `/model` handler: `grep -rn "/model\|def.*model_command" opencomputer/agent/`.
- [ ] Add `custom:` branch: parse spec, look up `config.custom_providers`, call `build_custom_provider`, dispatch via existing `on_model_swap` callback.
- [ ] On unknown name: error message listing available custom_providers.
- [ ] Commit: `feat(wave3): /model custom:<name>:<model_id> slash dispatch`.

### Task 4: Per-provider request_timeout + stream stale-watchdog

**Files:** `plugin_sdk/provider_contract.py`, `plugin_sdk/__init__.py`, `extensions/anthropic-provider/provider.py`, `extensions/openai-provider/provider.py`, `opencomputer/agent/loop.py`, `tests/test_provider_timeouts.py`

- [ ] Failing tests for `BaseProvider.request_timeout_seconds == 60.0`, `stale_timeout_seconds == 60.0`, subclass override works, `StreamStaleException` raised when stream idles longer than stale_timeout.
- [ ] Add fields to `BaseProvider` (class attributes; default 60.0 each; stale=None for opt-out).
- [ ] Add `StreamStaleException(TimeoutError)` to provider_contract.py with provider_name + stale_seconds.
- [ ] Re-export from `plugin_sdk/__init__.py`.
- [ ] Wire `request_timeout_seconds` into anthropic-provider's httpx.Client(timeout=...) and openai-provider's. Run those extensions' tests.
- [ ] Implement watchdog in `agent/loop.py`: track `last_token_at`, run consumer + watchdog as concurrent asyncio.Tasks, raise StreamStaleException if `time.monotonic() - last_token_at > stale_timeout`.
- [ ] Test with mock streaming response that yields once then awaits forever.
- [ ] Commit: `feat(wave3): per-provider request_timeout + stream stale watchdog`.

### Task 5: `oc model add/list/remove` Typer wizard

**Files:** `opencomputer/cli.py`, `tests/test_oc_model_cli.py`

- [ ] Failing tests using `CliRunner`: `oc model add NAME --base-url URL --key-env ENV --no-probe` writes entry; `oc model list` prints entries; `oc model remove NAME` deletes.
- [ ] Read existing helpers in `config_store.py`: `_atomic_write_yaml`, `_load_yaml`, `_config_path` (or whatever they're actually named).
- [ ] Add `model_app = typer.Typer()` group; subcommands `add`, `list`, `remove`.
- [ ] `add`: append to `cfg["custom_providers"]`, refuse duplicate name, optionally probe `/v1/models` with httpx.
- [ ] `list`: pretty-print `name  base_url`.
- [ ] `remove`: filter list, exit 1 if name not found.
- [ ] All writes via existing atomic helper.
- [ ] Commit: `feat(wave3): oc model add/list/remove CLI for custom_providers`.

### Task 6: Per-model context_length override flowing into compaction

**Files:** `opencomputer/agent/compaction.py` (or wherever context-length resolves), `tests/test_custom_provider_context_length.py`

- [ ] Locate resolver: `grep -rn "context_length\|get_context_length" opencomputer/agent/`.
- [ ] Failing test: `resolve_context_length(cfg, provider="custom:local", model="qwen")` returns the per-model override.
- [ ] Add early branch: if provider starts with `custom:`, look up `config.custom_providers[name].models[model_id].context_length` first; fall through to existing chain otherwise.
- [ ] Commit: `feat(wave3): per-model context_length override under custom_providers`.

### Task 7: PR-1 validation

- [ ] Full suite: `pytest tests/ -q --ignore=tests/test_honcho_default.py 2>&1 | tail -10`
- [ ] Ruff: `ruff check opencomputer/ plugin_sdk/ tests/ 2>&1 | tail -5`
- [ ] Push branch: `git push -u origin feat/hermes-wave3-provider-config-2026-05-08`

---

## PR-2: Provider features — xAI cache + HF suffixes + OR routing

### Task 8: API-contract verification (BLOCKING — research only)

- [ ] Use `mcp__plugin_context7_context7__query-docs` or web search to verify HF Inference Providers routing API: does `:fastest` map to a request body field (e.g. `provider: "auto"`), header, or URL query param?
- [ ] Verify OpenRouter `/v1/chat/completions` body shape for `provider:` block — does it accept the structure shown in reference doc verbatim?
- [ ] Document findings in `docs/refs/hermes-agent/2026-05-08-routing-api-contracts.md`.
- [ ] If divergent from spec assumptions, edit Tasks 9 / 10 / 11 inline before coding.

### Task 9: xAI `x-grok-conv-id` header

**Files:** `extensions/xai-provider/provider.py`, `tests/test_xai_cache_header.py`

- [ ] Failing test using respx: assert `x-grok-conv-id` is in captured request headers; same RuntimeContext.session_id → same conv_id; no session_id → per-process UUID.
- [ ] Read xai-provider's actual class structure first.
- [ ] In `__init__`, set `self._process_conv_id = str(uuid.uuid4())`.
- [ ] Override `_request_headers()` (or wrap the request-building method): inject `x-grok-conv-id` from `runtime.session_id` or fallback to `_process_conv_id`.
- [ ] Commit: `feat(wave3): xAI auto-attach x-grok-conv-id for KV cache reuse`.

### Task 10: HuggingFace routing suffixes

**Files:** `extensions/huggingface-provider/provider.py`, `tests/test_hf_routing_suffixes.py`

- [ ] Failing tests for `_parse_routing_suffix("model:fastest")` → `("model", "fastest")`, unknown suffix `:beta` passes through, no-colon model unchanged.
- [ ] Define `_KNOWN_HF_PROVIDERS = {"groq", "together", "fireworks", "replicate", "sambanova", "hyperbolic", "novita", "cerebras"}` and `_HF_ROUTING_SUFFIXES = {"fastest", "cheapest"} | _KNOWN_HF_PROVIDERS`.
- [ ] Implement `_parse_routing_suffix` using `rpartition(":")`.
- [ ] Inject suffix into request body / header per Task 8 finding.
- [ ] Test: respx mock asserts the field shows up in the captured body.
- [ ] Commit: `feat(wave3): HuggingFace :fastest/:cheapest/:provider routing suffixes`.

### Task 11: OpenRouter routing block + `:nitro`/`:floor`

**Files:** `opencomputer/agent/config.py` (add `ProviderRoutingConfig`), `opencomputer/agent/config_store.py`, `extensions/openrouter-provider/provider.py`, `tests/test_openrouter_routing.py`

- [ ] Failing tests: respx capture asserts `body["provider"] == {"sort": "price", "only": ["Anthropic"], ...}`; `_strip_routing_suffix("anthropic/claude-sonnet-4:nitro")` → `("anthropic/claude-sonnet-4", "nitro")`; suffix overrides config-block sort.
- [ ] Add `ProviderRoutingConfig` dataclass: `sort | None`, `only | ignore | order` tuples, `require_parameters bool`, `data_collection | None`.
- [ ] Wire YAML parsing under `model.provider_routing:`.
- [ ] In openrouter-provider: `_OR_ROUTING_SUFFIXES = {"nitro": "throughput", "floor": "price"}`.
- [ ] `_strip_routing_suffix(model)` using rpartition.
- [ ] In request body builder: strip suffix, build `provider_block` dict from routing config + suffix override (`suffix wins`), inject as `body["provider"]`.
- [ ] Commit: `feat(wave3): OpenRouter provider_routing config + :nitro/:floor suffixes`.

### Task 12: PR-2 validation

- [ ] Full suite + ruff. Push.

---

## PR-3: Reliability + MCP + mlx-whisper

### Task 13: `FallbackProvider` dataclass + config

**Files:** `opencomputer/agent/config.py`, `opencomputer/agent/config_store.py`, `tests/test_fallback_providers.py`

- [ ] Failing tests for FallbackProvider construction; `fallback_providers:` YAML roundtrip.
- [ ] `FallbackProvider(provider: str, model: str, base_url: str | None = None, key_env: str | None = None)` — provider may be `"openrouter"` or `"custom:<name>"`.
- [ ] Add `fallback_providers: tuple[FallbackProvider, ...] = ()` to Config.
- [ ] Parse YAML under top-level `fallback_providers:`.
- [ ] Commit: `feat(wave3): FallbackProvider dataclass + config schema`.

### Task 14: Cross-provider fallback resolver

**Files:** `opencomputer/agent/fallback.py`, `tests/test_fallback_providers.py`

- [ ] Read existing `agent/fallback.py` to understand current single-provider chain.
- [ ] Failing test (asyncio): primary mocked to raise 429 on first call → fallback_providers[0] (different provider) is invoked → response surfaces; assert messages list passed verbatim; assert per-turn scope (next user turn restarts at primary).
- [ ] Build chain: `[(primary, primary_model)] + [(build_provider(fp), fp.model) for fp in fallback_providers] + [(last_provider, m) for m in fallback_models]`.
- [ ] Iterate chain on `RateLimitError` / `ServiceError` / `httpx.HTTPStatusError(401|403|404)` / malformed.
- [ ] Re-raise final exception if entire chain exhausts.
- [ ] Commit: `feat(wave3): cross-provider fallback chain (per-turn scoped)`.

### Task 15: `oc fallback` Typer subcommand

**Files:** `opencomputer/cli.py`, `tests/test_oc_fallback_cli.py`

- [ ] Failing tests: `oc fallback` (no subcommand) lists chain; `oc fallback add openrouter/anthropic/claude-sonnet-4` appends; `oc fallback add custom:local/qwen3.5:27b` handles custom: prefix correctly; `oc fallback remove 0` removes by index.
- [ ] Implement `_split_provider_model(spec)` — handles `custom:name/model` and `provider/model` forms (slash-after-name).
- [ ] `fallback_app = typer.Typer()` with `callback(invoke_without_command=True)` for the bare list, plus `add`/`remove`/`clear` subcommands.
- [ ] All writes via `_atomic_write_yaml`.
- [ ] Commit: `feat(wave3): oc fallback CLI manager (list/add/remove/clear)`.

### Task 16: Per-server MCP tool filter

**Files:** `opencomputer/agent/config.py` (MCPServerConfig), `opencomputer/mcp/client.py`, `tests/test_mcp_tool_filter.py`

- [ ] Failing tests: `tools_allow=("read_file", "grep")` keeps only those; `tools_deny=("write_file",)` removes that one; `tools_allow=()` deny-all; `tools_allow=None` no-filter.
- [ ] Add `tools_allow: tuple[str, ...] | None = None` and `tools_deny: tuple[str, ...] = ()` to MCPServerConfig (config.py:333).
- [ ] Implement `_filter_tools(server_cfg, tools)` in `mcp/client.py`.
- [ ] Wire into `MCPClient.list_tools()` (or wherever tools register from MCP servers).
- [ ] Commit: `feat(wave3): per-server MCP tools_allow/tools_deny filter`.

### Task 17: mlx-whisper STT backend

**Files:** `opencomputer/voice/stt_mlx_whisper.py` (new), `opencomputer/voice/stt.py`, `pyproject.toml`, `tests/test_stt_mlx_whisper.py`

- [ ] Failing tests: backend `is_available()` is False on non-Apple-Silicon; transcribe smoke test gated `@pytest.mark.skipif` to Apple Silicon + mlx_whisper installed.
- [ ] Implement `MLXWhisperBackend` class with `name = "mlx-whisper"`, `is_available()` (checks `platform.system() == "Darwin"` + `platform.machine() == "arm64"` + import success), `transcribe(audio_path, model="mlx-community/whisper-large-v3-turbo")`.
- [ ] In `stt.py`, register the backend so `voice.stt: "mlx-whisper"` selects it.
- [ ] Add optional extra: `voice-mlx = ["mlx-whisper>=0.4.0"]` in `pyproject.toml`.
- [ ] Commit: `feat(wave3): mlx-whisper STT backend for Apple Silicon`.

### Task 18: PR-3 validation

- [ ] Full suite + ruff. Push.

---

## Post-flight

### Task 19: Open 3 PRs

- [ ] PR-1 against main (squash 6 commits): "feat(wave3): custom_providers + per-provider timeouts + oc model wizard"
- [ ] PR-2 against PR-1 head (or main if PR-1 merged): "feat(wave3): xAI cache + HF suffixes + OpenRouter provider_routing"
- [ ] PR-3: "feat(wave3): fallback_providers + oc fallback CLI + MCP tool filter + mlx-whisper STT"

### Task 20: Cleanup

- [ ] After all 3 PRs merge: `git worktree remove ~/Vscode/claude/OpenComputer-wave3 && git branch -D feat/hermes-wave3-provider-config-2026-05-08`

---

## Self-review

**Spec coverage:** all 10 features (A1, A2, B, C, D, E, F1, F2, G, H) → at least one task each. ✓

**Placeholder scan:** Task 8 (API verification) is a real research blocker, not a placeholder. Test descriptions name concrete imports and assertions. ✓

**Type consistency:** `CustomProvider` / `CustomProviderModelOverride` / `FallbackProvider` / `ProviderRoutingConfig` / `MCPServerConfig` / `StreamStaleException` named consistently across tasks. Helper functions `parse_custom_model_spec`, `build_custom_provider`, `_strip_routing_suffix`, `_filter_tools`, `_split_provider_model`, `_parse_routing_suffix` named consistently. ✓

**Open execution-time research:**
- Task 8 (API verification) blocks Tasks 9-11.
- Plugin import paths in `extensions/` may use hyphen-vs-underscore conventions — verify by reading `extensions/<name>/__init__.py` before coding.
- `_atomic_write_yaml` / `_load_yaml` / `_config_path` helper names — confirm by reading `config_store.py`.
