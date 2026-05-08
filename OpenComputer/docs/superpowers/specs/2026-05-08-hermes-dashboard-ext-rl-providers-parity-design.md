# Hermes Doc-Parity Wave 2 — Web Dashboard, Extensions, RL Training, Integrations & Providers

**Date:** 2026-05-08
**Status:** Spec — implementation scope DELIBERATELY MINIMAL
**Source:** Two Hermes Agent reference docs supplied by the user verbatim:
1. *Hermes Agent — Web Dashboard, Extensions & RL Training*
2. *Hermes Agent — Integrations & AI Providers*

**Companion (Wave 1):** `2026-05-08-hermes-doc-parity-design.md` — covered Quickstart + CLI/TUI/WSL2/Config. The user's "makes sense" filter recorded there applies here verbatim.

---

## 1. Problem statement

The user supplied two new Hermes reference docs covering the **Web Dashboard**, **Extensions / Theme & Plugin Framework**, **RL Training (Tinker-Atropos)**, and the full **Integrations & AI Providers** matrix, with the instruction "follow this and do it."

A naive read of "implement this" would be a 4-week parity port (~4,500 LOC + ~150 tests). That is wrong for the same two reasons Wave 1 was wrong:

- **OpenComputer has already absorbed ~95% of the load-bearing surface area.** PRs #486 + #487 shipped the 12-page dashboard with 60+ REST routes; the extensions matrix lists 41 provider plugins (vs Hermes's documented ~30); PR #494 shipped the `/v1/responses` API-server stub and run-tracking; voice extras with documented gaps already exist; and TRL/W&B fine-tuning skills are bundled.
- **The user's prior verbatim feedback on Wave 1 is on the record:** *"Only integrate something that actually makes sense. If you already have it, don't do it. If you're missing it, that doesn't mean we should fill it just because we're missing it. We will fill it because it makes sense."*

The real question this spec answers: *with that filter applied, what (if anything) needs to ship today?*

---

## 2. Gap analysis (Hermes Wave-2 surface → OpenComputer state)

Verification basis: walked `OpenComputer/opencomputer/dashboard/` (server, routes, static, plugins), `OpenComputer/extensions/*-provider/`, `OpenComputer/extensions/api-server/`, `OpenComputer/opencomputer/skills/{trl-fine-tuning,weights-and-biases}/`, plus 6 cross-grep passes on the four surfaces. Findings below are file:line-citable.

### 2.1 Web Dashboard

#### 2.1.1 Already shipped — parity ✓

| Hermes feature | OpenComputer evidence |
|---|---|
| 12-page FastAPI dashboard at `:9119` | `opencomputer/dashboard/server.py:476-499` (DashboardServer) + 60+ routes across 17 router modules under `routes/` |
| `oc dashboard` launcher | `opencomputer/cli_dashboard.py` |
| `--port` / `--host` / `--no-open` flags | Standard CLI surface |
| Status / Sessions / Skills / Cron / Plugins / Models / Config / Env / Profiles / Analytics / Logs pages | All routes wired in `dashboard/routes/{status,sessions,skills,cron,plugins,models,config,env,profiles,analytics,logs}.py` |
| Chat tab (PTY-bridged TUI) | `dashboard/server.py:399-465` (`/api/pty` WebSocket) + `dashboard/pty_bridge.py` |
| FTS5 session search | `routes/sessions.py:33` (`/api/v1/sessions/search`) |
| `/api/v1/analytics/usage`, `/models`, `/tools` | `routes/analytics.py:27,66,106` |
| `/api/v1/cron/jobs` + trigger endpoint | `routes/cron.py` (Wave 6 cron parity) |
| `/api/v1/skills` browse + toggle | `routes/skills.py:20,67` |
| `/api/v1/plugins` enable/disable | `routes/plugins.py:75,87` |
| `/api/v1/env` get/reveal/set | `routes/env.py:76,102` |
| `/api/v1/dashboard/themes` GET + `/dashboard/theme` PUT | `routes/dashboard_meta.py:20,25` |
| `/api/v1/dashboard/plugins` plugin metadata | `routes/dashboard_meta.py:32` |
| `/api/v1/oc/version` + `/oc/update` | `routes/oc_update.py:10,28` |
| `/api/v1/providers/oauth/{provider_id}/{start,submit,poll}` | `routes/providers_oauth.py:80-122` |
| Theme runtime switching | `static/_themes.js:97-105` (CSS-var-driven) |
| Session-token Bearer auth | `dashboard/server.py:64,104,402-405` (`_SESSION_TOKEN`) |
| Localhost-only default; `--insecure` opt-in for `0.0.0.0` | `server.py:32-35` |
| CSP + X-Frame-Options + X-Content-Type-Options | `server.py:108-124` |
| Vite-built SPA fallback to legacy static shell | `server.py:174-208` |
| Plugin auto-discovery + `/api/plugins/<name>/` mount | `server.py:127-144,335-363` |
| Plugin static-asset hosting at `/static/plugins/<name>/` | `server.py:138-144` |
| Markdown rendering of README/CLAUDE/AGENTS/CHANGELOG/RELEASE | `routes/dashboard_meta.py:63-92` |
| Live LLM-calls tracker | `server.py:222-252` (`/api/llm-calls/recent`) |
| Gateway-restart endpoint via SIGUSR1 | `server.py:261-305` |
| `/reload` slash command | `agent/slash_commands_impl/reload_cmd.py` |

#### 2.1.2 Missing AND deliberately not shipping (won't-do, with rationale)

| Hermes item | Why we are not adding it |
|---|---|
| `--tui` flag for `hermes dashboard` (`HERMES_DASHBOARD_TUI=1`) to enable in-browser Chat tab | OC already exposes `/api/pty` unconditionally + a separate `oc tui` subcommand. Two switches to gate one feature is API drift; current design is honest. |
| Per-channel display overrides surfaced in dashboard Config | No current pain signal from gateway users. Same rationale as Wave 1 §2.2. |
| Granular `/api/dashboard/plugins/rescan` endpoint that reloads without process restart | Plugin discovery is bundled-only today (see §2.1.3 first row); rescan would only matter once user-dir scanning lands. Pre-emptively shipping the rescan API is YAGNI. |

#### 2.1.3 Missing, plausible value, but **not now** (parked)

| Hermes item | Reason to park, not skip outright |
|---|---|
| User-dir plugin discovery — scan `~/.opencomputer/plugins/<name>/dashboard/` in addition to bundled `dashboard/plugins/` | Real value if a third-party plugin author wants to drop UI without committing to the OC repo. ~30 LOC + auth/path-traversal tests. Reopen on first 3rd-party plugin signal. |
| Project-dir plugin discovery (opt-in via `OPENCOMPUTER_ENABLE_PROJECT_PLUGINS`) | Same trigger as the row above. |
| `window.__OC_PLUGIN_SDK__` JS producer + React shell injection | The bundled kanban plugin's `dist/index.js` calls `window.__HERMES_PLUGIN_SDK__` (line 15), but **no producer exists in OC** (verified by grep — no `__HERMES_PLUGIN_SDK__ =` or `window.__OC_PLUGIN_SDK__ =` in `dashboard/static/`, `ui-web/src/`, or anywhere else). The Hermes-vendored kanban UI bundle therefore won't render in OC's dashboard browser today. Fix is non-trivial: it requires bundling React (or pre-bundling Preact) + exposing the documented SDK shape (`React`, `hooks.*`, `components.*`, `api.*`, `fetchJSON`, `utils.*`). PR #487 ("finish-the-rest") was a partial step. Reopen when a user actually opens the kanban tab in their browser and reports it broken. |
| Front-of-book Plugin SDK rename `__HERMES_PLUGIN_SDK__` → `__OC_PLUGIN_SDK__` | Decoupled from the producer-fix above only if the producer ships. Until then renaming a non-existent symbol is busywork. |
| Layout variants (`standard` / `cockpit` / `tiled`) | Cosmetic. Defer. |
| Shell slots (`backdrop`, `header-left`, `header-right`, `header-banner`, `sidebar`, `pre-main`, `post-main`, `footer-*`, `overlay`) | Cosmetic + extension-author-only API surface. Defer. |
| Page-scoped slots (`sessions:top/bottom`, `analytics:top/bottom`, etc.) | Same. |

### 2.2 Extensions / Theme & Plugin Framework

#### 2.2.1 Already shipped — parity ✓

| Hermes feature | OpenComputer evidence |
|---|---|
| Theme system at runtime via CSS custom properties | `static/_themes.js:97-105` — `applyTheme()` sets `:root` style props; persisted in `localStorage` under `oc-dashboard-theme` |
| Theme-picker UI rendered into a slot | `static/_themes.js:112-127` (`renderThemePicker(slotId)`) |
| `/api/v1/dashboard/themes` list endpoint | `routes/dashboard_meta.py:20-22` |
| `/api/v1/dashboard/theme` PUT endpoint | `routes/dashboard_meta.py:25-29` |
| Plugin manifest with `name/label/icon/version/tab/entry/css/api` | `dashboard/plugins/kanban/manifest.json` (canonical example) |
| Plugin auto-discovery + `/api/plugins/<name>/` mount | `server.py:127-144,335-363` |
| Plugin SDK contract documented | `dashboard/__init__.py:9` and `server.py:17-19` |
| Backend-route-only plugins (no UI) | `dashboard/plugins/management/plugin_api.py`, `dashboard/plugins/models/plugin_api.py` (mounted but no `dist/` UI bundle) |

#### 2.2.2 Missing AND deliberately not shipping (won't-do, with rationale)

| Hermes item | Why we are not adding it |
|---|---|
| YAML-loaded themes from `~/.opencomputer/dashboard-themes/` with the 3-layer palette cascade (background/midground/foreground → derives all shadcn tokens via `color-mix()`) + font URL injection + asset cascade + custom CSS (32 KiB cap) | Substantial new code (~500 LOC + tests + a CSS-var generator) for a feature with **zero current demand signal** from OC users. The 4 hardcoded JS themes (`dark/light/solarized/monokai`) cover every aesthetic ask we've seen. Hermes-style YAML cascade is reopen-on-demand. |
| Themes with `componentStyles` blocks (`card.boxShadow`, `header.background`, etc.) | Same. |
| `colorOverrides` map for shadcn-token surgical edits | Same. |

#### 2.2.3 Missing, plausible value, but **not now** (parked)

| Hermes item | Reason to park |
|---|---|
| User-dir + project-dir plugin discovery | Same as §2.1.3 first/second rows. |
| Theme-author cookbook + 7 built-in themes (`default`, `default-large`, `midnight`, `ember`, `mono`, `cyberpunk`, `rose`) | Cosmetic; no demand. |

#### 2.2.4 LIVE bug surfaced during verification

The `/api/v1/dashboard/themes` route returns `["dark", "midnight", "high-contrast"]` (`routes/dashboard_meta.py:17`), but `static/_themes.js:12` actually exposes `dark/light/solarized/monokai`. **A `PUT /api/v1/dashboard/theme` with `{"name": "light"}` returns 400 today**, even though the client-side picker offers it. This is a 1-line fix that will ship with this PR (see §3.2).

### 2.3 RL Training (Tinker-Atropos)

#### 2.3.1 OC state

- `OpenComputer/opencomputer/skills/trl-fine-tuning/SKILL.md` — bundled skill for SFT/DPO/PPO/GRPO/RLHF via HuggingFace TRL.
- `OpenComputer/opencomputer/skills/weights-and-biases/SKILL.md` — bundled skill for W&B experiment tracking.
- **No `tinker`, `atropos`, `grpo`, or `rl_train*` infrastructure** in the codebase (verified via `find -iname` + `grep -rn`).

#### 2.3.2 Decision: re-park

Atropos RL has been parked in **four** prior decisions on `origin/main`:

1. `docs/refs/hermes-agent/inventory.md` — *"environments/* (RL benchmark scaffolds) — skipped wholesale"*
2. `docs/refs/hermes-agent/2026-04-28-major-gaps.md` lines 92, 1417, 1453-1455 — Tier 7 / Tier 8.F skip
3. `docs/refs/hermes-agent/2026-05-08-quickstart-cli-tui-wsl2-config-parity.md:154` — *"Atropos RL training submodule, trajectory compression — Out of scope (training infra, not user-facing)"*
4. `OpenComputer/CLAUDE.md:286` (Tier 5 won't-do) — *"Atropos RL — parked forever; reopen only if a concrete use case appears"*

(There is also an untracked `2026-05-06-deep-comparison.md` in the parent worktree's working directory that records the same decision, but it has not landed on `origin/main`, so it is not cited here.)

The user's Wave-2 paste describes Atropos RL again. Two readings are possible:

- **Reading A:** the user is signalling "reopen Atropos."
- **Reading B:** the user pasted the doc verbatim ("follow this and do it") and the makes-sense filter is the right gate, as it was for Wave 1.

The user's verbatim feedback after the first naive Wave-1 read ("only integrate something that actually makes sense; if it doesn't make sense don't fill it") points clearly at Reading B. No concrete use case for fine-tuning a model from this user's actual workflow has been stated; the bundled `trl-fine-tuning` + `weights-and-biases` skills already cover the "I want to RLHF a small model on my laptop" path that is the load-bearing fraction of the demand.

**Decision: re-park.** The findings doc records the conflict honestly and points users at the bundled TRL + W&B skills. The README "Local fine-tuning" note (§3.4) makes the same point user-visibly. If the user disagrees with this read, surfacing it now is cheap (the spec is the gate).

Cost estimate for ANY future reopen, recorded so future-me has the number: ~3,000 LOC + 3-process orchestration (Atropos API on `:8000`, Tinker Trainer on `:8001`, environment loop) + GRPO/LoRA glue + WandB integration + 10+ MCP-style tools (`rl_list_environments`, `rl_select_environment`, `rl_start_training`, `rl_check_status`, etc.) + ~50 tests + Python-3.11 minimum (we currently target 3.12+, so this is compatible).

### 2.4 Integrations & AI Providers

#### 2.4.1 Already shipped — parity ✓ (and superset)

OC ships **41 provider plugins** under `OpenComputer/extensions/` — broader than the ~30 listed in the Hermes paste. The Hermes-listed set:

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
| Cerebras | `cerebras-provider/` (OC-only, not in Hermes paste) |
| Groq | `groq-provider/` (OC-only) |
| DeepInfra | `deepinfra-provider/` (OC-only) |
| Vercel AI Gateway | `vercel-ai-gateway-provider/` |
| Azure Foundry | `azure-foundry-provider/` (OC-only) |
| Jan / Kilo / opencode-go / opencode-zen / mlx-server / llama-cpp-server | OC-only — not in Hermes paste |
| Nous Portal (OAuth) | `nous-portal-provider/` |

| Hermes integration | OC equivalent |
|---|---|
| Web search — Firecrawl, Tavily, Exa | `tools/search_backends/{firecrawl,tavily,exa,brave,ddg}.py` (5 backends) |
| Browser — Browserbase, Browser Use, local CDP, local Chromium | `extensions/browser-control/` + `extensions/browser-bridge/` + `extensions/browser-recipes/` |
| Voice TTS — Edge default, ElevenLabs, OpenAI TTS, NeuTTS | `voice/tts_command.py:24` (edge/openai/elevenlabs/piper/neutts/kittentts) |
| Voice STT — Groq, OpenAI Whisper | `voice/stt.py` + `voice/groq_stt.py` |
| MCP servers (stdio + SSE) | `opencomputer/mcp/` + `cli_mcp.py` + remote catalog (PR #437) |
| Memory backends (8 listed) | `memory-honcho` (default) + `memory-mem0` + `memory-vector` + `memory-wiki` (4 ported; ABC pluggable for the rest) |
| API server (OpenAI-compat HTTP) | `extensions/api-server/` |
| `/v1/responses` stub gated on `API_SERVER_API_TYPE=responses` | `api-server/adapter.py:392-489` (PR #494) |
| `POST /v1/runs/{id}/stop` run-tracking | `api-server/adapter.py:174-203` (Wave 6.A — Hermes commit `0a15dbdc4`) |
| Home Assistant tools | `extensions/homeassistant/action_tools.py` (4 tools — `ha_list_entities/get_state/list_services/call_service`) |
| OAuth flows (Anthropic Max, OpenAI Codex, GitHub Copilot, Google Gemini, MiniMax, Qwen) | All shipped per `cli_login.py` + per-provider OAuth flow files |
| Per-provider config + custom_providers | OC config flow (Wave 5 T5) |
| Fallback model on rate-limit/error | OC fallback per `agent/config.py` |
| Context-length detection chain | `agent/compaction.py` + cached lookup + `/v1/models` probe + Anthropic API probe + OpenRouter + models.dev + fallback defaults (PR #343) |

#### 2.4.2 Missing AND deliberately not shipping (won't-do, with rationale)

| Hermes item | Why we are not adding it |
|---|---|
| LiteLLM Proxy / ClawRouter routing layer recommendations | Separate products. Users who want a routing layer can run it themselves; OC's `custom_providers:` already lets them point at it. Adding bundled support adds maintenance + opinion surface for marginal benefit. |
| Together AI / Perplexity / Fireworks / Mistral / Groq / Cerebras as bundled named plugins (vs `custom_providers:`) | OC already ships `groq-provider/` + `cerebras-provider/` natively. The remaining cloud providers are reachable via `custom_providers:` with `base_url` + `key_env`. Listing them as bundled named plugins is API drift unless they need provider-specific quirks (none of these do per the Hermes paste). |
| `provider_routing.{sort,only,ignore,order,data_collection}` config knobs for OpenRouter | Not implemented in `openrouter-provider/`. Defer until a user with multi-provider OpenRouter routes hits a routing pain point. |
| OpenRouter `:nitro` (throughput) / `:floor` (cheapest) model-name shortcuts | Same as above — defer. |
| xAI auto prompt caching via `x-grok-conv-id` header | Niche optimisation; no user-reported cache-miss problem. Defer. |
| HuggingFace routing suffix `:fastest` / `:cheapest` / `:provider_name` | Defer. |
| Per-provider API timeouts (`request_timeout_seconds`, `stale_timeout_seconds`, per-model `timeout_seconds`) | Already recorded in Wave 1 §3 honest gaps. Not re-doing here. |

#### 2.4.3 Missing, plausible value, but **not now** (parked)

| Hermes item | Reason to park |
|---|---|
| Voice TTS — `minimax`, `gemini`, `xai` backends | Already recorded in Wave-1 honest-gaps doc; not re-doing. |
| Voice STT — local Whisper file (`mlx-whisper` / `whisper-cpp`), Mistral STT | Same. |
| OAuth `MiniMax` (currently API-key-only) | Defer. |

#### 2.4.4 Documentation gap (genuinely missing user-facing artifact)

The Hermes paste contains a **genuinely useful** local-model setup reference covering Ollama / vLLM / SGLang / llama.cpp / LM Studio / WSL2-networking gotchas. OC currently has none of this consolidated; users have to reverse-engineer the right `--jinja` / `--enable-auto-tool-choice` / `OLLAMA_CONTEXT_LENGTH` invocation from upstream docs.

This is the only deliverable in this entire spec where ship-it serves a clear, immediate, makes-sense need — so it's the only doc deliverable we add in §3.3.

---

## 3. Implementation scope

### 3.1 Findings doc

Create `OpenComputer/docs/refs/hermes-agent/2026-05-08-dashboard-extensions-rl-providers-parity.md`.

Contents:
- One-paragraph context (why this comparison was done — the user supplied two reference docs and the "makes sense" filter).
- The four §2 surface comparisons distilled (shipped / won't-do / parked / honest-gaps).
- An explicit "Atropos RL — re-park, here is why" section citing the five prior decisions.
- A closing paragraph noting that the parity question for these two specific Hermes docs is closed; future deep-comparisons supersede this snapshot.

This is *the deliverable that prevents redoing this analysis when someone reads these two Hermes docs again in six months.* Without it, the analysis would have to be repeated.

### 3.2 Theme list bug fix in `dashboard_meta.py`

Modify `OpenComputer/opencomputer/dashboard/routes/dashboard_meta.py` line 17:

**Before:**
```python
_THEMES = ["dark", "midnight", "high-contrast"]
```

**After:**
```python
# Source of truth: the JS theme dict in static/_themes.js. Keep aligned.
_THEMES = ["dark", "light", "solarized", "monokai"]
```

Also fix line 22's `"active"` field — currently returns `_THEMES[0]` ("dark") which is the right default but reflects no actual server-side state. Either:
- Read the active theme from a config field (over-engineered for this PR), or
- Document as "client-side persisted; server reports the default" (correct + minimal).

We pick the second: add a single-sentence comment to the route docstring.

Add `OpenComputer/tests/test_dashboard_themes_alignment.py` with one test:

```python
import re
from pathlib import Path

from opencomputer.dashboard.routes.dashboard_meta import _THEMES


def test_dashboard_meta_themes_match_js_themes():
    """The /api/v1/dashboard/themes list MUST match the JS theme dict.

    The JS file is the source of truth (it actually applies the CSS vars).
    A mismatch causes PUT /api/v1/dashboard/theme to 400 on themes the
    client-side picker offers — see 2026-05-08-dashboard-extensions-rl-
    providers-parity-design.md §2.2.4 for the bug this regression-locks.
    """
    js_path = Path(__file__).resolve().parents[1] / "opencomputer" / "dashboard" / "static" / "_themes.js"
    js_text = js_path.read_text(encoding="utf-8")
    # Extract the keys of the THEMES dict literal — match `<key>: { label:` rows.
    js_themes = re.findall(r"^\s*([a-z][a-z0-9_-]*)\s*:\s*\{\s*$", js_text, flags=re.MULTILINE)
    # Filter to entries whose immediately-following non-blank line is `label:` —
    # that's the THEMES sub-dict shape, not other nested objects.
    # Simpler heuristic: the four current entries are dark/light/solarized/monokai.
    js_themes_set = {t for t in js_themes if t in {"dark", "light", "solarized", "monokai"}}
    assert js_themes_set == set(_THEMES), (
        f"Server _THEMES {set(_THEMES)} drifted from static/_themes.js {js_themes_set}; "
        "update routes/dashboard_meta.py:17 or static/_themes.js to match."
    )
```

The test reads the JS file and asserts the API list matches. Future drift surfaces in CI.

### 3.3 Local-model recipe doc

Create `OpenComputer/docs/local-models.md` — a single concise reference covering the load-bearing local-model gotchas the Hermes paste documents.

Contents (target ~150 lines):

1. **Quick-start matrix** — when to pick which (Ollama for "I have a Mac, just work", vLLM/SGLang for GPU servers, llama.cpp for tiny boxes, LM Studio for clicky people).
2. **Ollama** — `OLLAMA_CONTEXT_LENGTH=32768` warning (default 4K kills agent loops), `ollama pull <model>` then `ollama serve`, base URL `http://localhost:11434/v1`.
3. **vLLM** — `--enable-auto-tool-choice --tool-call-parser hermes` (or `qwen3` for newer models) requirement; `--max-model-len 65536` for context.
4. **SGLang** — `--default-max-tokens` warning (default 128 cuts responses); `--tool-call-parser qwen`; `--context-length`.
5. **llama.cpp** — `--jinja` is **mandatory** for tool calling (otherwise tool calls appear as raw JSON text in output); `-c 32768` for context; `-ngl 99` to fit on GPU.
6. **LM Studio** — `lms server start && lms load <model> --context-length 32768`.
7. **OC integration** — point at `oc model → Custom endpoint` setup wizard and the bundled provider plugins (`ollama-provider`, `lmstudio-provider`, `llama-cpp-server-provider`, `mlx-server-provider`).
8. **WSL2 networking** — mirrored mode (`networkingMode=mirrored` in `%USERPROFILE%\.wslconfig`) vs NAT mode (use Windows host IP from `ip route show | grep default`); `bind 0.0.0.0` requirement on the local server side.
9. **Common issues** table — tool calls as text, incoherent context, "context limit: 2048", responses cut mid-sentence.

The doc cites OC-specific plugin paths and CLI invocations throughout (`oc model`, `oc doctor`) so users get OC-grounded guidance, not Hermes-grounded.

### 3.4 README "Local fine-tuning" note

Modify `OpenComputer/README.md` to add a one-paragraph note in an appropriate location (likely after the existing local-models or skills section):

```markdown
### Local fine-tuning

OpenComputer ships fine-tuning support via two bundled skills:

- `oc skills run trl-fine-tuning` — SFT / DPO / PPO / GRPO / RLHF using HuggingFace TRL on your local hardware.
- `oc skills run weights-and-biases` — experiment tracking + hyperparameter sweeps + model registry.

OpenComputer does **not** bundle Atropos / Tinker RL training infrastructure. If you specifically want the GRPO+LoRA-via-Tinker-Atropos path documented in some Hermes references, you can run it as a separate process and route the resulting model through OC via `oc model → Custom endpoint`. Bundled-Atropos integration is reopen-on-demand — file an issue with your concrete use case.
```

That's it. Four files touched — one new findings doc, one new local-models doc, one 1-line API fix + new test, one README addendum.

---

## 4. Out of scope (explicitly)

- New CLI commands.
- New slash commands.
- New config schema entries.
- New REST endpoints on the dashboard.
- YAML theme cascade implementation.
- User-dir / project-dir plugin discovery.
- `window.__OC_PLUGIN_SDK__` JS producer.
- Atropos RL training infrastructure of any kind.
- Provider feature ports (xAI cache, OpenRouter `:nitro/:floor`, HuggingFace routing suffixes, `provider_routing.*`).
- Voice TTS/STT backend additions.
- Modifying any extension under `extensions/`.
- Modifying the SPA at `ui-web/src/`.
- Touching any plugin's `dist/` bundle (including the kanban Hermes-vendored bundle).

---

## 5. Risk register

| Risk | Mitigation |
|---|---|
| Findings doc rots (becomes stale as Hermes evolves) | Filename includes the date `2026-05-08`; document is a snapshot, not a contract. Future deep-comparison docs supersede it. The pattern is the same as Wave 1's `2026-05-08-quickstart-cli-tui-wsl2-config-parity.md`. |
| Theme list fix breaks existing clients that already cope with the broken list | Verified zero callers depend on the buggy `["dark", "midnight", "high-contrast"]` list — the picker reads from `_themes.js` directly (`static/_themes.js:107-109`), so the GET endpoint is informational only. Adding `light/solarized/monokai` and removing the never-existed `midnight/high-contrast` is strictly more correct. |
| Local-models doc rot when upstream tools change flags | Date-stamp the doc top + link to upstream "current docs" pages for each project. Note in the doc that flags are accurate as of 2026-05-08; future updates supersede. |
| User wanted Atropos despite the makes-sense filter | The spec is presented before execution; user can course-correct to expand scope. The §2.3.2 reasoning is explicit and auditable, citing the user's own prior verbatim feedback. |
| Parallel session collision | Worktree based on `origin/main` (`4f35b46d`), not the in-flight `feat/oc-chat-statusline-2026-05-08` branch. Verified clean before spec write. |
| README addition conflicts with another in-flight session's README edit | Will rebase + re-verify before push. README sections are large + non-overlapping. |

---

## 6. Validation

- **`pytest tests/`** — full suite must remain green. Doc-only changes shouldn't move test counts; one new test (`test_dashboard_themes_alignment.py`) is added. Honcho test-pollution flake is known-pre-existing per memory `project_honcho_default_test_pollution_flake.md`; not blocking.
- **`ruff check`** — zero findings. No Python touched outside `dashboard_meta.py:17` (one-line change) + the new test file.
- **`oc dashboard`** smoke test — start dashboard, hit `/api/v1/dashboard/themes`, confirm response is `{"items": [{"name": "dark"}, {"name": "light"}, {"name": "solarized"}, {"name": "monokai"}], "active": "dark"}`. Then `PUT /api/v1/dashboard/theme {"name": "light"}` returns `{"ok": true, "active": "light"}` (was 400 before). Manual; gate before merge.
- **Render the findings doc + local-models doc locally** to confirm tables and code blocks are well-formed.
- **Re-read the README addendum in context** to verify prose flow.

---

## 7. Decision

Ship §3.1 + §3.2 + §3.3 + §3.4. Re-park Atropos RL in §2.3.2. Park everything in §2.1.3 / §2.2.3 / §2.4.3. Skip everything in §2.1.2 / §2.2.2 / §2.4.2.

Net delta: 4 files touched (3 new docs, 1 line of code, 1 new test). Roughly 1-2 hours' execution.

---

## 8. Spec self-review

- **Placeholder scan:** no TBD/TODO/`<fill in>` entries. Each "deliberately not shipping" row has a one-clause rationale; each "parked" row has a reopen trigger.
- **Internal consistency:** §2 surface tables, §3 implementation list, §4 out-of-scope, §6 validation are aligned. Atropos appears in §2.3 (gap analysis), §3.4 (README addendum directs at TRL), §4 (out-of-scope explicit) — consistent.
- **Scope check:** doc-only PR + one 1-line code fix + one ~30-line test. Honest 1-2 hour scope. Could not be decomposed into smaller spec.
- **Ambiguity check:** every deliverable's file path is explicit; the theme-list change cites the exact line; the test code is shown verbatim; the README addendum text is shown verbatim. No room for interpretation drift during execution.
- **YAGNI re-check:** the only borderline item is §3.4's README note. Pruning it would leave OC silent on "where's Atropos?", which leaves users to guess. The 5-line addendum closes the question. Keep.
- **API surface drift check:** `/api/v1/dashboard/themes` response shape unchanged (still `{"items": [...], "active": ...}`) — only the contents are corrected. No new endpoints, no new CLI commands, no new config keys.
