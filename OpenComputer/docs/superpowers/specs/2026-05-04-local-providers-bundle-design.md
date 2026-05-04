# Local Providers Bundle — Design + Plan (Phase 12d.5 partial)

**Date:** 2026-05-04
**Status:** combined spec+plan
**Reference:** CLAUDE.md §5 Tier-2 Phase 12d.5 — local-providers plugin port

---

## 1. Goal

Add 4 OpenAI-compatible local-LLM provider plugins, mirroring the Cerebras/DeepInfra pattern from PR #419. Single PR, 4 self-contained commits.

Today OC has `ollama-provider` + `ollama-cloud-provider`. Adding the 4 most-used local-LLM runtimes:

| # | Plugin | Default endpoint | Notes |
|---|---|---|---|
| 1 | `llama-cpp-server-provider` | `http://localhost:8080/v1` | llama.cpp's `llama-server` binary |
| 2 | `lmstudio-provider` | `http://localhost:1234/v1` | LM Studio desktop app |
| 3 | `jan-provider` | `http://localhost:1337/v1` | Jan.ai desktop app |
| 4 | `mlx-server-provider` | `http://localhost:8081/v1` | `mlx_lm.server` (port 8081 to avoid llama.cpp clash) |

---

## 2. Karpathy verification

- ✅ None of `extensions/{llama-cpp-server,lmstudio,jan,mlx-server}-provider/` exist (grep confirmed)
- ✅ All 4 expose OpenAI-compatible `POST /v1/chat/completions` per their docs
- ✅ Cerebras provider at 189 LOC is the canonical template
- ✅ Each is local-only — no upstream auth required by default; user can set `<NAME>_API_KEY` if they've configured one
- ✅ User's `iteration_timeout_s=600` issue: independent (PR #449 in flight)

---

## 3. Implementation pattern (per provider)

Cookie-cutter from PR #419 Cerebras. Each commit creates 4 files:

```
extensions/<name>-provider/
├── plugin.json     # manifest with setup.providers[].env_vars
├── plugin.py       # register(api) — dual-import pattern
└── provider.py     # OpenAI-compat HTTP client
tests/test_<name>_provider.py
```

Each `provider.py`:
- ABC: `BaseProvider`
- `_api_key()` returns env value OR empty string (local default — no auth)
- `complete()` POSTs to `<base>/chat/completions` with OpenAI-shape body
- `stream_complete()` aiohttp SSE parser, yields `text_delta` events
- `list_models()` returns hardcoded common-model defaults; user can override via env

**Auth differences from Cerebras (which required an API key):**
- Local providers: `_api_key()` returns `""` if env var not set, and the request omits the `Authorization` header. If the user HAS set an env var (e.g. they configured a token in LM Studio), it's sent as `Bearer`.

---

## 4. Per-provider details

### 4.1 llama-cpp-server-provider

- Env: `LLAMA_CPP_SERVER_API_KEY` (optional), `LLAMA_CPP_SERVER_BASE_URL` (override `http://localhost:8080/v1`)
- Default models: `("local-model",)` — llama-server doesn't enumerate; user overrides via `LLAMA_CPP_SERVER_MODEL`
- Signup: `https://github.com/ggerganov/llama.cpp/tree/master/examples/server`

### 4.2 lmstudio-provider

- Env: `LMSTUDIO_API_KEY` (optional, default `lm-studio` — LM Studio's literal default), `LMSTUDIO_BASE_URL` (override `http://localhost:1234/v1`)
- Default models: `("local-model",)` — same reason
- Signup: `https://lmstudio.ai`

### 4.3 jan-provider

- Env: `JAN_API_KEY` (optional), `JAN_BASE_URL` (override `http://localhost:1337/v1`)
- Default models: Jan exposes `/v1/models` — but for v1 we hardcode `("local-model",)` and document the env override
- Signup: `https://jan.ai`

### 4.4 mlx-server-provider

- Env: `MLX_SERVER_API_KEY` (optional), `MLX_SERVER_BASE_URL` (override `http://localhost:8081/v1`)
- Default port `8081` to avoid conflict with llama-cpp (`8080`)
- Default models: `("mlx-community/Llama-3.1-8B-Instruct-4bit",)`
- Signup: `https://github.com/ml-explore/mlx-examples/tree/main/llms`

---

## 5. Tests (one file per provider, ~6 tests each = 24 total)

Each `test_<name>_provider.py` mirrors `test_cerebras_provider.py`:

1. module-loads
2. default base URL constant
3. reads API key from env
4. handles missing API key (returns empty string instead of raising — local default)
5. default models list
6. mocked complete() hits correct endpoint + Bearer header (when key set) OR no auth header (when empty)

---

## 6. Out of scope

- Auto-discovery of running local servers (port-scan + version probe)
- Default-model auto-detection via `/v1/models`
- HTTPS / TLS for remote local-network setups
- Streaming-tool-calls (not implemented in any local server reliably)

---

## 7. Self-audit

**Risks:**
1. **Port conflict between llama-cpp (8080) and mlx (8081)** — addressed by picking different defaults; user can override either via env.
2. **Local server not running at probe time** — provider raises httpx.ConnectError, surfaced as a clear error in CLI/loop. No retry logic in v1 (out of scope).
3. **Missing auth header when API key empty** — must NOT send `Authorization: Bearer ` (empty bearer). Tests verify.
4. **Module-name collision** — each provider uses a unique `_load_provider_module` test helper name. Pattern from Cerebras already handles this.
5. **Cookie-cutter copy errors** — biggest risk is forgetting to swap a constant when copying Cerebras → llama-cpp. Mitigation: per-task tests verify the URL, env var name, and model list.

**Edge cases:**
- Empty API key + bearer-required local server → user sets env var; documented in plugin.json signup_url comment.
- Multimodal request with image_url block → strip non-text per `Message.content` flatten convention from Cerebras provider.

**Defensible? Yes.** 4 commits, ~600 LOC, ~3-4 hours.
