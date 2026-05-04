# OpenClaw-since-April-23 Selective Port — Design Spec

**Date:** 2026-05-04
**Status:** Draft → ready for review
**Sources compared:**
- Current: `/Users/saksham/Vscode/claude/sources/openclaw` (HEAD `d841394eba`, 2026-05-03)
- Snapshot: `/Users/saksham/Vscode/claude/sources/openclaw-2026.4.23` (snapshot of 2026-04-23)

---

## 1. Goal

Port six high-leverage improvements from openclaw into OpenComputer. Each is **net-new value to OC**, **zero overlap with the in-flight Wave 5 Hermes import**, and **self-contained** (no cross-item dependencies). The six items fit into one focused PR.

---

## 2. Why this scope (and not more, not less)

### What I considered porting (full candidate list)

The diff surfaced 15 entirely-new openclaw extensions and ~6,976 commits of churn. Candidate list, ranked by leverage:

| # | Item | Decision | Reason |
|---|------|----------|--------|
| 1 | `/steer` queue-independent steering | **SKIP** | Wave 5 T3 already shipping it |
| 2 | `/side` alias for `/btw` | **PORT** | OC already has `/btw`; trivial alias adds polish |
| 3 | Tool denylist short-circuit | **PORT** | OC has `python_safety` denylist, but no config-level `tools.deny`; this is a real gap |
| 4 | Per-plugin hook `timeoutMs` config | **PORT** | OC's hook engine has no per-plugin timeout knob; reliability win |
| 5 | Cerebras provider | **PORT** | New cloud LLM provider; OC has 32 already, format is well-known |
| 6 | DeepInfra provider | **PORT** | Same as Cerebras — new cloud LLM provider |
| 7 | Web readability mode | **PORT** | OC's `web_fetch` uses BeautifulSoup; readability extraction yields cleaner article text |
| 8 | File-transfer plugin (paired-node binary file ops) | **DEFER** | Depends on openclaw's "paired node" concept which OC doesn't have (~2-week port) |
| 9 | Tree-sitter shell command explainer | **DEFER** | Adds tree-sitter dep; benefit hinges on approval-UI surface OC doesn't expose yet |
| 10 | Streaming `progress` mode unified across channels | **DEFER** | Touches every channel adapter; large blast radius, low marginal value |
| 11 | Diagnostics-prometheus | **DEFER** | Useful long-term; requires HTTP metrics endpoint — separate PR |
| 12 | Migrate-claude / migrate-hermes plugins | **SKIP** | OC isn't a target for migration *from* itself; not applicable |
| 13 | Bonjour LAN discovery | **DEFER** | Requires paired-node concept; same blocker as file-transfer |
| 14 | Lazy session-row gate | **SKIP** | Wave 5 D17 already shipping it |
| 15 | Hook duration_ms field | **SKIP** | Wave 5 T14 already shipping it |
| 16 | Codex extension uplift (~30 files) | **SKIP** | Wave 5 already covered Codex provider; further uplift is post-Wave-5 |
| 17 | Web-readability extension (whole new ext) | **PORT (folded into #7)** | Bring readability *capability* into existing `web_fetch` rather than spinning a parallel extension |
| 18-25 | Document-extract, azure-speech, senseaudio, inworld, gradium, swabble, sdk pkg, google-meet | **DEFER** | Each is a substantial extension on its own; punt to follow-up PRs |

### Sequencing rationale

These 6 items are **deliberately decoupled**:
- /side touches only `slash_commands.py` + adds 1 alias
- Denylist short-circuit is one method on `ToolRegistry`
- Hook timeout is two fields on `HookSpec` + one wrap in `HookEngine.fire_blocking`
- Cerebras/DeepInfra are new directories under `extensions/`
- Web readability adds an optional dependency to the existing `web_fetch.py`

**No item blocks any other.** Implementation can run in parallel per-item; commits land per-item.

---

## 3. Detailed designs

### 3.1 `/side` alias for `/btw`

**Problem:** openclaw added `/side` as a text + native-slash alias for `/btw`. Users who don't think to type "by the way" find `/side` more discoverable. OC's `BtwCommand` is fully functional; we just need a parallel registration.

**Approach:** Extend the SlashCommand ABC with an optional `aliases: tuple[str, ...] = ()` field. The dispatcher and registry consult both `name` and `aliases` when resolving an incoming slash. No behavior change for commands that don't declare aliases.

**Files:**
- `plugin_sdk/slash_command.py` — add `aliases` field to ABC
- `opencomputer/agent/slash_commands_impl/btw_cmd.py` — set `aliases = ("side",)`
- `opencomputer/agent/slash_commands.py` — extend `dispatch_slash` resolution to check aliases (and `is_slash_command` to recognize aliases as slash commands)
- `opencomputer/cli_ui/slash_completer.py` — surface aliases in completion (each alias becomes a separate completion entry pointing at the same command)
- Test: `tests/agent/test_slash_aliases.py`

**Out of scope:** Multi-word aliases. Aliases that conflict with existing primary names (rejected at registration with clear error).

### 3.2 Config-level tool denylist with factory short-circuit

**Problem:** OC has runtime denylists baked into specific tools (python_safety, applescript). It does not have a top-level `agent.tools.deny: ["WebSearch", "Bash"]` config that universally blocks tool registration *and* skips the optional factory work for blocked tools (avoiding cold-start cost for tools that will be filtered out before the model can see them).

**Approach:** Add `agent.tools.deny: list[str]` to config (matching openclaw's `tools.deny` shape). At `ToolRegistry.register()` time, check the deny list — if the tool's `schema.name` is denied, skip registration entirely (don't construct the instance, don't call lazy-init). Add `ToolRegistry.is_denied(name)` helper for callers that want to short-circuit factory work *before* construction.

**Files:**
- `opencomputer/agent/config.py` — add `ToolsConfig.deny: list[str]` field (default `[]`)
- `opencomputer/tools/registry.py` — `register()` consults config, skips denied tools; add `is_denied(name)` static helper
- `opencomputer/tools/__init__.py` (or wherever bulk tool registration lives) — wrap each optional factory call in `if not ToolRegistry.is_denied(name): register(...)`
- Test: `tests/test_tool_denylist.py`

**Out of scope:** Wildcard patterns (`tools.deny: ["WebFetch*"]`). Group denial (`group:fs`). These are openclaw extensions that aren't worth the surface for the first cut.

### 3.3 Per-plugin hook `timeoutMs` config

**Problem:** OC's `HookEngine.fire_blocking()` has no timeout. A slow or hung hook can wedge the agent loop indefinitely. Openclaw exposes `plugins.entries.<id>.hooks.timeoutMs` so operators can bound this without patching plugin code.

**Approach:**
- Extend `HookSpec` with optional `timeout_ms: int | None = None` (None = no timeout, current behavior).
- Wrap each `await spec.callback(ctx)` call inside `fire_blocking` in `asyncio.wait_for(..., timeout=spec.timeout_ms / 1000)` when set. On timeout: log a warning, treat as `pass` (don't block tool execution).
- Add `plugins.<id>.hooks.timeout_ms` reader in `opencomputer/plugins/loader.py`. When a plugin registers a hook via `PluginAPI.register_hook`, the loader injects the configured timeout if set.

**Why fail-open on timeout?** Matches OC's existing exit-code-2 = block, anything else = fail-open hook contract (per CLAUDE.md §7). A wedged hook must never wedge the loop.

**Files:**
- `plugin_sdk/hooks.py` — add `timeout_ms` field to `HookSpec`
- `opencomputer/hooks/engine.py` — wrap callback in `asyncio.wait_for` when `spec.timeout_ms` set
- `opencomputer/plugins/loader.py` — read `plugins.<id>.hooks.timeout_ms` from config and apply at registration time
- `opencomputer/plugins/registry.py` (`PluginAPI.register_hook`) — accept optional timeout override
- Test: `tests/test_hook_timeout.py`

### 3.4 Cerebras provider (`extensions/cerebras-provider/`)

**Problem:** Cerebras Inference offers very-fast inference for Llama / Qwen / GPT-OSS. Not currently in OC's 32-provider catalog.

**Approach:** Standard provider extension following the OpenAI-compatible shape (Cerebras exposes an OpenAI-compatible HTTP API at `https://api.cerebras.ai/v1`). Subclass `BaseProvider` from `plugin_sdk`, reuse OC's existing `httpx`-based streaming client pattern.

**Files (new dir):**
- `extensions/cerebras-provider/plugin.json` — manifest (kind=provider, auth env=`CEREBRAS_API_KEY`)
- `extensions/cerebras-provider/plugin.py` — `register(api)` entry
- `extensions/cerebras-provider/provider.py` — `CerebrasProvider(BaseProvider)`
- Test: `tests/test_cerebras_provider.py`

**Models exposed (defaults; user-overridable via `CEREBRAS_MODEL`):**
- `llama-3.3-70b`
- `llama3.1-8b`
- `qwen-3-32b`
- `gpt-oss-120b`

### 3.5 DeepInfra provider (`extensions/deepinfra-provider/`)

**Problem:** DeepInfra hosts ~100 open-weights models with OpenAI-compatible API at `https://api.deepinfra.com/v1/openai`. Not currently in OC's 32-provider catalog.

**Approach:** Mirror Cerebras structure; only the base URL and default model list differ.

**Files (new dir):** Same shape as Cerebras.

**Models exposed (defaults):**
- `meta-llama/Meta-Llama-3.3-70B-Instruct`
- `Qwen/Qwen3-235B-A22B`
- `deepseek-ai/DeepSeek-V3`

### 3.6 Web readability mode for `web_fetch`

**Problem:** OC's `web_fetch` uses BeautifulSoup to strip script/style/nav tags. This is good for "I want everything on the page" but noisy for article reading: nav, footer, comments, sidebars all bleed through. openclaw's `web-readability` extension uses Mozilla's Readability algorithm to extract just the article body.

**Approach:** Add an optional `mode: "auto" | "full" | "readability" = "auto"` parameter to `web_fetch`. In `auto` mode, use heuristics (URL patterns matching news/blog/article/docs domains → readability; everything else → full). Implement readability via the `readability-lxml` Python port (mature, no JS runtime needed).

**Files:**
- `opencomputer/tools/web_fetch.py` — add `mode` parameter, branch on it; new `_html_to_article()` helper using `readability-lxml`
- `pyproject.toml` — add `readability-lxml>=0.8` to deps (it's small, ~25KB, pure-Python wrapper around lxml which OC already pulls in transitively via beautifulsoup4)
- Test: `tests/test_web_fetch_readability.py`

**Tradeoff considered:** Could pull in `trafilatura` instead (more accurate, broader format support) but it adds ~3MB and 8 transitive deps. `readability-lxml` is the right tool for the first cut; switch later if accuracy becomes a complaint.

---

## 4. Architecture diagram

```
┌─────────────────────────────────────────────────────────┐
│ User: types /side <text>  OR  /btw <text>               │
└──────────────┬──────────────────────────────────────────┘
               │
               ▼
   ┌───────────────────────────────────────────┐
   │ slash_commands.dispatch_slash             │
   │   resolve(name) → first match in:         │
   │     1. command.name                       │  ← NEW: also check
   │     2. command.aliases                    │     aliases tuple
   └─────────────┬─────────────────────────────┘
                 ▼
         ┌───────────────┐
         │ BtwCommand    │
         │   .execute()  │  ← unchanged
         └───────────────┘

────────────────────────────────────────────────────────────

┌────────────────────────────────────────────┐
│ Plugin loader: registers hook              │
│   PluginAPI.register_hook(spec)            │
└────────────┬───────────────────────────────┘
             │  inject timeout from config:
             │    plugins.<id>.hooks.timeout_ms
             ▼
   ┌─────────────────────┐
   │ HookSpec(           │
   │   ...,              │
   │   timeout_ms=5000,  │  ← NEW field
   │ )                   │
   └─────────┬───────────┘
             ▼
   ┌─────────────────────────────────────┐
   │ HookEngine.fire_blocking(ctx)       │
   │   for spec in ordered_specs:        │
   │     if spec.timeout_ms:             │
   │       await wait_for(               │  ← NEW: timeout wrap
   │         spec.callback(ctx),         │
   │         timeout=spec.timeout_ms/1k) │
   │     else:                           │
   │       await spec.callback(ctx)      │  ← unchanged
   └─────────────────────────────────────┘

────────────────────────────────────────────────────────────

┌────────────────────────────────────────────┐
│ ToolRegistry.register(tool)                │
│   if config.agent.tools.deny:              │  ← NEW
│     if tool.schema.name in deny: SKIP      │
│     ───────────────────────────────────    │
│   else: existing path                      │
└────────────────────────────────────────────┘

────────────────────────────────────────────────────────────

┌─────────────────────────────────────────────┐
│ web_fetch(url, mode="auto")                 │
│                                             │
│   if mode == "readability":                 │  ← NEW branch
│     html → readability-lxml → article text  │
│   elif mode == "full":                      │
│     html → BeautifulSoup → all visible text │
│   else  # auto:                             │
│     guess based on URL pattern              │
└─────────────────────────────────────────────┘
```

---

## 5. Testing strategy

| Item | Test approach |
|------|---------------|
| `/side` alias | Unit: register BtwCommand, call dispatch_slash("side foo") and dispatch_slash("btw foo") — both resolve to same command instance. Edge: register two commands where one's alias collides with another's name → registration error. |
| Tool denylist | Unit: with config `tools.deny=["WebFetch"]`, register WebFetch → registry has 0 entries. Without deny: 1 entry. Idempotency: register twice, deny applies both times. |
| Hook timeout | Unit: register hook that `await asyncio.sleep(2)` with timeout_ms=100 → `fire_blocking` returns None (pass). Unit: timeout_ms=None → existing behavior unchanged. Unit: configured timeout in plugin config overrides default. |
| Cerebras provider | Unit: mock httpx.AsyncClient, verify request hits `api.cerebras.ai/v1`, auth header has `Bearer $CEREBRAS_API_KEY`. Stream parse: SSE chunks accumulate into expected ProviderResponse. |
| DeepInfra provider | Same shape as Cerebras. |
| Web readability | Unit: feed a captured HTML fixture (real article from a news site) → `mode="readability"` returns just article body, no nav/footer. `mode="full"` returns BeautifulSoup output (current behavior). `mode="auto"` on news URL pattern → readability path. |

All tests use existing patterns (pytest, monkeypatch, httpx mock, no network).

---

## 6. Out of scope (explicitly deferred)

These items were considered and rejected for **this** PR. They may land in follow-ups:

- **File-transfer plugin** — depends on openclaw's "paired node" concept; OC's gateway/wire model differs significantly. ~2-week port.
- **Tree-sitter shell explainer** — adds tree-sitter as a dep; the feature targets approval UIs OC doesn't expose yet.
- **Streaming `progress` mode** — touches every channel adapter; large surface, low marginal user value vs. current per-channel streaming.
- **Diagnostics-prometheus** — useful for production OC deployments; needs separate HTTP metrics endpoint design.
- **migrate-claude / migrate-hermes** — not applicable; OC isn't migrating *from* itself.
- **document-extract, azure-speech, senseaudio, inworld, gradium, swabble, google-meet** — each is a substantial standalone extension; punt to follow-ups based on user demand.

---

## 7. Open questions (pre-implementation)

None blocking. Two minor decisions baked in:

1. **Aliases shape — tuple vs list.** Going with `tuple[str, ...]` (immutable, signals "set at class definition, not mutated").
2. **Auto-mode heuristic for web_fetch readability.** First cut: regex match on URL host/path against a small set (`*.medium.com`, `*.substack.com`, `/blog/`, `/article/`, `/news/`, `/posts/`). Documented as a simple list — future expansion is just appending patterns.

---

## 8. Self-audit (executed before showing this design)

### What might be wrong with this scope?

- **Risk:** Six items might be too many for one PR. Counter: each is small (≤200 LOC), zero cross-dependencies. Reviewer can mentally chunk per-item.
- **Risk:** Cerebras/DeepInfra need API keys for live testing. Counter: all tests mock httpx; no live calls in CI.
- **Risk:** `readability-lxml` may not handle all article shapes well. Counter: caller can fall back to `mode="full"`; auto-mode only triggers on known article-domain patterns.
- **Risk:** Adding `aliases` to SlashCommand ABC is a subtle contract change. Counter: it has a default value (`()`), so existing commands and plugins compile unchanged.
- **Risk:** Tool denylist short-circuit changes registration semantics. Counter: tools that aren't in `deny` behave identically; only denied tools see new behavior (silent skip).
- **Risk:** Hook timeout fail-open could mask real bugs. Counter: matches existing OC hook contract (CLAUDE.md §7); we log a warning so operators see the timeout.

### What edge cases might bite?

1. **Alias name collision with primary name** — explicit registration error. Tested.
2. **Tool denylist case sensitivity** — match on exact `schema.name` (case-sensitive), as is convention in OC. Documented.
3. **Hook timeout = 0** — interpret as "no timeout" (same as None) to avoid the gotcha of `wait_for(t, timeout=0)` raising immediately. Documented.
4. **Cerebras/DeepInfra streaming** — both use SSE; reuse OC's existing OpenAI-compatible SSE parser. No new infrastructure needed.
5. **Readability extraction returns empty** — fall through to full BeautifulSoup mode and warn in the logs. Tested.

### Was anything missed from the diff?

Re-checking the new-extensions list against the rejected pile:
- `gradium` — looked into it: appears to be a graphics/rendering helper for openclaw's macOS/iOS apps. **Not portable to OC.**
- `swabble` — a Swift-only test box. **Not portable.**
- `senseaudio` — audio sensing for openclaw's voice features. **OC's voice path differs**; punt.
- `inworld` — Inworld AI voice provider. **Useful** but new vendor; defer to a follow-up.

No surprises. Six items is the right cut.

### Defensible? Yes.

Approach B (selective port, 6 items, 1 PR) is the maximum-leverage cut: each item is something OC users will notice within a week of using the binary, none requires changing OC's core architecture, and all six can be reviewed in one sitting.
