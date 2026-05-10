# Hermes Doc-Parity Snapshot — 2026-05-08 — Dashboard, Extensions, RL, Providers

**Source docs compared:**
1. *Hermes Agent — Web Dashboard, Extensions & RL Training*
2. *Hermes Agent — Integrations & AI Providers*

**OpenComputer state walked:** main tip `4f35b46d` + this PR's branch.
**Verification basis:** Direct file:line walks of `OpenComputer/opencomputer/dashboard/`, `OpenComputer/extensions/*-provider/`, `OpenComputer/extensions/api-server/`, `OpenComputer/opencomputer/skills/{trl-fine-tuning,weights-and-biases}/`, plus 6 cross-grep passes for SDK exposure / theme producers / RL infra.
**Companion (Wave 1):** `2026-05-08-quickstart-cli-tui-wsl2-config-parity.md`.

---

## How this document was written

The user supplied two Hermes reference docs verbatim. The user's prior verbatim feedback on Wave 1 is still load-bearing: *"Only integrate something that actually makes sense. If you already have it, don't do it. If you're missing it, that doesn't mean we should fill it just because we're missing it."*

This rewrite applies that filter. ~95% of the surface is already shipped (PRs #486, #487, #494, plus all of Tier S / Wave 5 / Wave 6 prior work). The residual either (a) doesn't pass the makes-sense filter, (b) has been deliberately scoped out before, or (c) is a genuinely useful documentation gap (local-model recipes).

This PR ships **four** items that pass the filter: this findings doc, a 1-line dashboard themes API/JS divergence fix + alignment test, a `docs/local-models.md` reference, and a README "Local fine-tuning" note redirecting RL questions at the bundled TRL + W&B skills.

---

## 1. Web Dashboard

### 1.1 Confirmed shipped — parity ✓

| Hermes feature | OpenComputer evidence |
|---|---|
| 12-page FastAPI dashboard at `:9119` | `opencomputer/dashboard/server.py:476-499` (DashboardServer) + 60+ routes across 17 router modules |
| `oc dashboard` launcher | `opencomputer/cli_dashboard.py` |
| Status / Sessions / Skills / Cron / Plugins / Models / Config / Env / Profiles / Analytics / Logs pages | `dashboard/routes/{status,sessions,skills,cron,plugins,models,config,env,profiles,analytics,logs}.py` |
| Chat tab (PTY-bridged TUI) | `dashboard/server.py:399-465` (`/api/pty` WebSocket) + `dashboard/pty_bridge.py` |
| FTS5 session search | `routes/sessions.py:33` |
| Cron jobs + trigger | `routes/cron.py` (Wave 6 cron parity) |
| Plugin enable/disable | `routes/plugins.py:75,87` |
| Theme switching | `routes/dashboard_meta.py:25-29` + `static/_themes.js:97-105` |
| Plugin auto-discovery + `/api/plugins/<name>/` mount | `server.py:127-144,335-363` |
| Markdown rendering of README/CLAUDE/AGENTS | `routes/dashboard_meta.py:63-92` |
| Live LLM-calls tracker | `server.py:222-252` |
| Gateway-restart via SIGUSR1 | `server.py:261-305` |
| OAuth provider flow | `routes/providers_oauth.py:80-122` |
| Vite-built SPA fallback to legacy static shell | `server.py:174-208` |
| Session-token Bearer auth + CSP + X-Frame-Options | `server.py:64,108-124` |

### 1.2 LIVE bug found during verification

`/api/v1/dashboard/themes` returns `["dark", "midnight", "high-contrast"]` (`routes/dashboard_meta.py:17`), but `_themes.js:12` actually exposes `dark/light/solarized/monokai`. **`PUT /api/v1/dashboard/theme {"name":"light"}` returns 400 today** despite the client-side picker offering `light` as an option. This PR fixes the server list and adds an alignment test that fails if the two ever drift again.

### 1.3 Deliberately not shipping

| Hermes item | Why |
|---|---|
| `--tui` flag for `hermes dashboard` (in-browser Chat tab gating) | OC exposes `/api/pty` unconditionally; gating it behind a separate switch would be API drift. |
| Per-channel display overrides surfaced in dashboard Config | No current pain signal from gateway users (also Wave 1 §2.2). |
| Pre-emptive `/api/dashboard/plugins/rescan` | Plugin discovery is bundled-only today; rescan only matters once user-dir scanning lands. |

### 1.4 Parked — awaiting demand signal

| Hermes item | Reason to park |
|---|---|
| User-dir plugin discovery (`~/.opencomputer/plugins/<name>/dashboard/`) | Reopen on first 3rd-party plugin signal. ~30 LOC + auth/path-traversal tests. |
| Project-dir plugin discovery (`OPENCOMPUTER_ENABLE_PROJECT_PLUGINS`) | Same trigger. |
| `window.__OC_PLUGIN_SDK__` JS producer + React shell | The bundled kanban plugin's `dist/index.js` calls `window.__HERMES_PLUGIN_SDK__` (line 15), but **no producer exists in OC** (verified via grep — no `__HERMES_PLUGIN_SDK__ =` or `__OC_PLUGIN_SDK__ =` anywhere). The Hermes-vendored kanban UI bundle therefore won't render in OC's dashboard browser today. Fixing it is non-trivial (bundle React or Preact + expose the documented SDK shape — `React`, `hooks.*`, `components.*`, `api.*`). PR #487 was a partial step. Reopen when a user actually hits the kanban tab in their browser. |
| Hermes SDK rename (`__HERMES_PLUGIN_SDK__` → `__OC_PLUGIN_SDK__`) | Dependent on the producer fix above. Renaming a non-existent symbol is busywork. |
| Layout variants (`standard` / `cockpit` / `tiled`) + shell slots | Cosmetic; defer. |

---

## 2. Extensions / Theme & Plugin Framework

### 2.1 Shipped ✓

| Feature | OC evidence |
|---|---|
| Theme system at runtime via CSS custom properties | `static/_themes.js:97-105` |
| Theme picker UI rendered into a slot | `static/_themes.js:112-127` |
| `/api/v1/dashboard/themes` GET + `/dashboard/theme` PUT | `routes/dashboard_meta.py:20,25` |
| Plugin manifest with `name/label/icon/version/tab/entry/css/api` | `dashboard/plugins/kanban/manifest.json` |
| Plugin auto-discovery + `/api/plugins/<name>/` mount | `server.py:127-144,335-363` |
| Backend-route-only plugins (no UI) | `dashboard/plugins/management/`, `dashboard/plugins/models/` |

### 2.2 Deliberately not shipping

| Hermes item | Why |
|---|---|
| YAML-loaded themes from `~/.opencomputer/dashboard-themes/` with the 3-layer palette cascade (background/midground/foreground), font URL injection, asset cascade, custom CSS (32 KiB cap) | ~500 LOC + tests + a CSS-var generator for a feature with **zero current demand signal**. The 4 hardcoded JS themes cover every aesthetic ask we've seen. Reopen-on-demand. |
| `componentStyles`, `colorOverrides`, `customCSS` blocks | Same. |

### 2.3 Parked

Same as §1.4 user-dir / project-dir / SDK-producer rows. Plus the 7 built-in Hermes themes (`default`, `default-large`, `midnight`, `ember`, `mono`, `cyberpunk`, `rose`) — cosmetic; defer.

---

## 3. RL Training (Tinker-Atropos)

### 3.1 OC state

- `opencomputer/skills/trl-fine-tuning/SKILL.md` — bundled SFT/DPO/PPO/GRPO/RLHF via HuggingFace TRL.
- `opencomputer/skills/weights-and-biases/SKILL.md` — bundled W&B experiment tracking.
- **No `tinker`, `atropos`, `grpo`, or `rl_train*` infrastructure** in the codebase.

### 3.2 Decision: re-park

Atropos RL has been parked in four prior tracked decisions on `origin/main`:

1. `docs/refs/hermes-agent/inventory.md` — *"environments/* (RL benchmark scaffolds) — skipped wholesale"*
2. `docs/refs/hermes-agent/2026-04-28-major-gaps.md` lines 92, 1417, 1453-1455 — Tier 7 / Tier 8.F skip
3. `docs/refs/hermes-agent/2026-05-08-quickstart-cli-tui-wsl2-config-parity.md:154` — *"Atropos RL training submodule, trajectory compression — Out of scope (training infra, not user-facing)"*
4. `OpenComputer/CLAUDE.md:286` (Tier 5 won't-do) — *"Atropos RL — parked forever; reopen only if a concrete use case appears"*

The user's Wave 2 paste re-introduces the topic. With the user's explicit makes-sense filter applied, **no concrete fine-tuning use case from this user's actual workflow has been stated**, and the bundled `trl-fine-tuning` + `weights-and-biases` skills already cover the load-bearing fraction of the demand surface (small-model RLHF on local hardware).

**Re-parked.** The README addendum that ships with this PR directs users at the bundled TRL + W&B skills. If the user disagrees with this reading, the spec is the gate to course-correct — surfacing it now is cheaper than reverting a 3,000-LOC port.

Cost estimate for any future reopen, recorded so future-me has the number: ~3,000 LOC + 3-process orchestration (Atropos API on `:8000`, Tinker Trainer on `:8001`, environment loop) + GRPO/LoRA glue + WandB integration + 10+ MCP-style RL tools (`rl_list_environments`, `rl_select_environment`, `rl_start_training`, `rl_check_status`, etc.) + ~50 tests + Python 3.11 minimum (we currently target 3.12+, so this is compatible).

---

## 4. Integrations & AI Providers

### 4.1 Shipped ✓ (and superset)

OC ships **41 provider plugins** under `extensions/` — broader than the ~30 in the Hermes paste. Every provider listed in the Hermes paste has a native OC plugin:

| Hermes provider | OC plugin |
|---|---|
| Anthropic (API + OAuth) | `anthropic-provider/` |
| OpenAI (API + Codex OAuth) | `openai-provider/` + `codex-provider/` |
| OpenRouter | `openrouter-provider/` (Wave 5 T5) |
| Google Gemini (OAuth + API) | `gemini-oauth-provider/` + `gemini-provider/` |
| GitHub Copilot | `github-copilot-provider/` + `copilot-acp-provider/` |
| DeepSeek | `deepseek-provider/` |
| xAI (Grok) | `xai-provider/` |
| z.AI / GLM | `zai-provider/` |
| Kimi / Moonshot (+ China) | `kimi-provider/` + `kimi-china-provider/` |
| MiniMax (Anthropic-style + China + OAuth) | `minimax-anthropic-provider/` + `minimax-china-anthropic-provider/` |
| Alibaba/Qwen (OAuth + DashScope) | `qwen-oauth-provider/` + `dashscope-provider/` + `alibaba-coding-plan-provider/` |
| Hugging Face | `huggingface-provider/` |
| AWS Bedrock | `aws-bedrock-provider/` |
| Ollama (local + cloud) | `ollama-provider/` + `ollama-cloud-provider/` |
| NVIDIA NIM | `nvidia-nim-provider/` |
| GMI Cloud | `gmi-provider/` |
| StepFun | `stepfun-provider/` |
| Arcee AI | `arcee-provider/` |
| Xiaomi MiMo | `xiaomi-provider/` |
| Tencent TokenHub | `tencent-provider/` |
| LM Studio | `lmstudio-provider/` |
| Custom endpoint | `custom_providers:` config (Wave 5 T5) |
| Nous Portal (OAuth) | `nous-portal-provider/` |
| Vercel AI Gateway | `vercel-ai-gateway-provider/` |

OC additionally ships `cerebras-provider/`, `groq-provider/`, `deepinfra-provider/`, `azure-foundry-provider/`, `jan-provider/`, `kilo-provider/`, `opencode-go-provider/`, `opencode-zen-provider/`, `mlx-server-provider/`, `llama-cpp-server-provider/` — none in the Hermes paste.

| Hermes integration | OC equivalent |
|---|---|
| Web search — Firecrawl, Tavily, Exa | `tools/search_backends/` (5 backends total) |
| Browser — Browserbase, Browser Use, local CDP | `extensions/browser-control/`, `browser-bridge/`, `browser-recipes/` |
| Voice TTS — Edge default + ElevenLabs + OpenAI + NeuTTS | `voice/tts_command.py:24` (edge / openai / elevenlabs / piper / neutts / kittentts) |
| Voice STT — Groq, OpenAI Whisper | `voice/stt.py` + `voice/groq_stt.py` |
| MCP servers (stdio + SSE) | `opencomputer/mcp/` + `cli_mcp.py` + remote catalog (PR #437) |
| API server (OpenAI-compat HTTP) | `extensions/api-server/` |
| `/v1/responses` stub gated on `API_SERVER_API_TYPE=responses` | `api-server/adapter.py:392-489` (PR #494) |
| `POST /v1/runs/{id}/stop` run-tracking | `api-server/adapter.py:174-203` (Wave 6.A) |
| Home Assistant tools | `extensions/homeassistant/action_tools.py` |
| Memory backends (8 listed in Hermes paste) | `memory-honcho` + `memory-mem0` + `memory-vector` + `memory-wiki` + ABC pluggable |
| Context-length detection chain | `agent/compaction.py` (PR #343 — model-aware widths) |

### 4.2 Deliberately not shipping

| Hermes item | Why |
|---|---|
| LiteLLM Proxy / ClawRouter routing-layer recommendations | Separate products. Users who want a routing layer can run it themselves; OC's `custom_providers:` already lets them point at it. |
| Together AI / Perplexity / Fireworks / Mistral as bundled named plugins | All reachable via `custom_providers:` with `base_url` + `key_env`; listing them as bundled named plugins is API drift unless they need provider-specific quirks (none do per the Hermes paste). |
| `provider_routing.{sort,only,ignore,order,data_collection}` config knobs for OpenRouter | Not implemented; defer until a user with multi-provider OpenRouter routes hits a routing pain point. |
| OpenRouter `:nitro` / `:floor` model-name shortcuts | Defer. |
| xAI auto prompt caching via `x-grok-conv-id` header | Niche; no user-reported cache-miss problem. |
| HuggingFace routing suffix `:fastest` / `:cheapest` / `:provider_name` | Defer. |
| Per-provider API timeouts (request/stale/per-model) | Already recorded in Wave 1 §3 honest gaps. |

### 4.3 Parked

Voice TTS gaps (`minimax`, `gemini`, `xai` backends) and STT gaps (local Whisper file via `mlx-whisper`/`whisper-cpp`, Mistral STT) — already recorded in Wave 1 honest-gaps; not re-doing. MiniMax is API-key-only today (OAuth pending); defer.

### 4.4 Documentation gap (genuinely missing — this PR fills)

OC has **no consolidated local-model recipe doc**. Users hitting the Ollama context-length-4K-default trap, vLLM `--enable-auto-tool-choice` requirement, SGLang `--default-max-tokens` cap, llama.cpp `--jinja` mandatory-for-tool-calling rule, or LM Studio `lms server start` startup must reverse-engineer each from upstream docs. This PR ships `docs/local-models.md` covering all six (plus WSL2 networking).

---

## 5. What this PR ships

1. This findings doc.
2. `dashboard/routes/dashboard_meta.py:17` — `_THEMES = ["dark", "light", "solarized", "monokai"]` (was `["dark", "midnight", "high-contrast"]`) + `tests/test_dashboard_themes_alignment.py`.
3. `docs/local-models.md` — the 6-stack recipe doc.
4. `README.md` — 1-paragraph "Local fine-tuning" addendum directing at TRL + W&B skills, explicitly noting Atropos is reopen-on-demand.

Net delta: 4 files. ~1-2 hours' work.

---

## 6. Closing

This snapshot supersedes nothing — Wave 1's `2026-05-08-quickstart-cli-tui-wsl2-config-parity.md` and this Wave 2 doc together close the parity question for **all four** Hermes Agent reference docs the user supplied on 2026-05-08. Future deep-comparisons (when a new Hermes doc lands) supersede these snapshots; filenames are date-stamped so the supersession is clean.
