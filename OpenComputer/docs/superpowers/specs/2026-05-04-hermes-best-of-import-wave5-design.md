# Hermes Best-of Import — Wave 5 Design Spec

**Date:** 2026-05-04
**Author:** Saksham (via Claude / brainstorming skill)
**Status:** DRAFT — pending self-audit + user approval
**Predecessor:** PR #413 (`2026-05-03-hermes-best-of-import-design.md`)
**Approach:** B (Tier 1 selective port — 15 items)

---

## 1. Context

Since the 2026-04-23 hermes-agent snapshot we already ported into OC (PR #413: credential rotation + Codex + MCP attachments + ACP depth), upstream hermes-agent has shipped **245 feat/refactor commits** (~195 distinct headline features) up through 2026-05-04. Most are noise relative to OC's strategic direction; a handful are high-leverage and easy to port.

This spec ports **Tier 1 (14 items)** in a single PR with 4 grouped commits. Tier 2 (~30 items, including curator improvements which OC currently lacks the substrate for) and Tier 3 (skip) are catalogued in §8/§9 for future waves.

### Already in OC (skipped — no work needed)
- All 6 candidate providers (azure-foundry, minimax-anthropic, minimax-china-anthropic, tencent, vercel-ai-gateway, xai) — already in `extensions/`
- Codex provider, credential rotation, MCP attachments, ACP depth — PR #413
- Standing Orders, MCPOAuth, oc update, OpenRouter providers, model aliases — PRs #257-#263
- Voice mode (edge_tts + groq_stt), realtime voice, browser-control — earlier waves
- Skills hub + URL well_known + GitHub source — already shipped (only HTTP-direct URL source missing)

---

## 2. Tier 1 scope (14 items in 4 commit groups)

### Group A — Agent core (5 items, ~1,800 LOC)

| # | Item | Hermes ref | OC target | Port size |
|---|------|------------|-----------|-----------|
| A1 | `/goal` Persistent Goals (Ralph loop) — **complementary to existing Standing Orders (rev-2 PR #320), not duplicative**: Standing Orders are continuously-applied directives; `/goal` is a turn-budgeted completion loop that ends on judge-pass | `265bd59c1` | `opencomputer/agent/goal.py` (NEW), `cli_ui/slash_handlers.py` | ~500 |
| A2 | Tool-call loop guardrails (warning-first) | `58b89965c` + `0704589ce` | `opencomputer/agent/tool_guardrails.py` (NEW), `agent/loop.py` (MOD) | ~500 |
| A3 | `/steer` + `/queue` ACP slash commands | `e27b0b765` | `opencomputer/acp/server.py` (MOD), `acp/session.py` (MOD) | ~250 |
| A4 | `busy_ack_enabled` config + runtime-metadata footer | `2b512cbca` + `e123f4ecf` | `opencomputer/gateway/dispatch.py` (MOD), `gateway/runtime_footer.py` (NEW), slash `/footer` | ~350 |
| A5 | OpenRouter response caching (provider-side, **distinct from PR #339's prompt-cache work**) | `457c7b76c` | `extensions/openrouter-provider/openrouter_adapter.py` (MOD) | ~200 |

### Group B — Multimodal / Voice (4 items, ~1,400 LOC)

| # | Item | Hermes ref | OC target | Port size |
|---|------|------------|-----------|-----------|
| B1 | `video_analyze` tool | `c9a3f36f5` | `opencomputer/tools/video_analyze.py` (NEW) — mirror `vision_analyze.py` | ~500 |
| B2 | Piper TTS native provider | `8d302e37a` | `opencomputer/voice/tts_piper.py` (NEW), `voice/tts.py` (MOD) | ~350 |
| B3 | TTS command-type provider registry | `2facea7f7` | `opencomputer/voice/tts_command.py` (NEW), `voice/tts.py` (MOD) | ~300 |
| B4 | Native `send_multiple_images` for major channels | `3de8e2168` + `04ea895ff` | `extensions/{telegram,discord,slack,mattermost,email,signal}/adapter.py` (MOD) | ~250 |

### Group C — Plugin platform (3 items, ~700 LOC)

| # | Item | Hermes ref | OC target | Port size |
|---|------|------------|-----------|-----------|
| C1 | `pre_gateway_dispatch` plugin hook | `1ef1e4c66` | `plugin_sdk/hooks.py` (MOD), `gateway/server.py` (MOD) | ~200 |
| C2 | `pre_approval_request` / `post_approval_response` hooks | `30307a980` | `plugin_sdk/hooks.py` (MOD), `opencomputer/agent/trust_ramp.py` + `agent/policy_audit.py` + `plugin_sdk/permission_mode.py` (MOD — fire hook around the dangerous-command gate, exact callsite resolved during implementation by tracing `trust_ramp.consent_for_command()`) | ~250 |
| C3 | `duration_ms` in `post_tool_call` + `transform_tool_result` | `59b56d445` | `plugin_sdk/hooks.py` (MOD), `agent/loop.py` tool dispatch site (MOD) | ~250 |

### Group D — Storage + Skills (2 items, ~600 LOC)

| # | Item | Hermes ref | OC target | Port size |
|---|------|------------|-----------|-----------|
| D1 | Lazy session creation (defer DB row until first message) | `c5b4c4816` | `opencomputer/agent/session_db.py` (MOD), `agent/loop.py` `_ensure_db_session()` gate | ~400 |
| D2 | Skill install from HTTP(S) URL | `9c416e20a` | `opencomputer/skills_hub/sources/url.py` (NEW), `skills_hub/router.py` (MOD) | ~200 |

### Group E — *(removed)*

E1 (curator consolidated/pruned classification) **deferred to Tier 2** — OC currently has no active curator analogous to hermes' `auxiliary.curator`. Skill maintenance in OC is on-demand via `skills_hub/installer.py` rather than a periodic background scanner. Adding a full curator is a Wave 6 candidate, not a Wave 5 cherry-pick.

**Total estimated:** ~4,500 LOC additions + ~600 LOC modifications across Groups A–D. Comparable to PR #413 (~3,700 LOC) — at the upper end but achievable.

---

## 3. Architecture

### 3.1 File layout

```
opencomputer/
  agent/
    goal.py                   ← NEW (A1) — GoalState dataclass, judge call, continuation loop
    tool_guardrails.py        ← NEW (A2) — loop detector + warning emitter
    loop.py                   ← MOD (A2/C3/D1) — guardrail hook, duration_ms hook, _ensure_db_session
    session_db.py             ← MOD (D1) — lazy create, prune_empty_ghost_sessions()
  tools/
    video_analyze.py          ← NEW (B1) — base64 video upload, OpenRouter video_url block
  voice/
    tts.py                    ← MOD (B2/B3) — register piper + command provider type
    tts_piper.py              ← NEW (B2) — lazy import, voice cache, ffmpeg conversion
    tts_command.py            ← NEW (B3) — placeholder substitution, shell-quote-aware
  gateway/
    dispatch.py               ← MOD (A4) — busy_ack_enabled gate, runtime footer append
    runtime_footer.py         ← NEW (A4) — pure-function footer renderer
    server.py                 ← MOD (C1) — fire pre_gateway_dispatch hook
  acp/
    server.py                 ← MOD (A3) — /steer + /queue command handlers
    session.py                ← MOD (A3) — interrupt/queue state machine
  cli_ui/
    slash_handlers.py         ← MOD (A1/A4) — /goal, /goal status|pause|resume|clear, /footer
  skills_hub/
    sources/
      url.py                  ← NEW (D2) — UrlSource for direct HTTP(S) SKILL.md
    router.py                 ← MOD (D2) — register UrlSource, dispatch order before WellKnown

plugin_sdk/
  hooks.py                    ← MOD (C1/C2/C3) — VALID_HOOKS additions, hook signatures

extensions/
  openrouter-provider/
    openrouter_adapter.py     ← MOD (A5) — X-OpenRouter-Cache headers + status logging
  telegram/, discord/, slack/, mattermost/, email/, signal/
    adapter.py                ← MOD (B4) — override send_multiple_images() with native batch API
```

### 3.2 Cross-cutting concerns

**Slash command registration.** OC uses `cli_ui/slash.py` registry. New commands `/goal`, `/footer`, `/steer`, `/queue`. ACP commands also registered in `acp/server.py` for the ACP transport.

**Plugin hooks.** OC's `plugin_sdk/hooks.py` already defines hook contracts. Three new entries to `VALID_HOOKS`:
- `pre_gateway_dispatch` (event, user) → action dict
- `pre_approval_request` (surface, command, reason) → ignored
- `post_approval_response` (surface, command, choice, reason) → ignored
- `duration_ms: int` kwarg added to existing `post_tool_call` + `transform_tool_result`

**Config schema.** `display.busy_ack_enabled`, `display.runtime_footer.enabled`, `openrouter.response_cache`, `openrouter.response_cache_ttl`, `tts.piper.voice`, `tts.providers.<name>` — all additive, all default safe.

**State persistence.** Goal state in `SessionDB.state_meta` keyed by `goal:<session_id>` (per hermes design). Tool guardrail state in-memory per turn (no persistence).

**Backward compatibility.** Every change is additive. Lazy session creation (D1) is the only behavior-changing item — old sessions still work, only new sessions defer. Migration: `prune_empty_ghost_sessions()` runs once on startup.

**Group ordering.** Groups A and D both modify `opencomputer/agent/loop.py`. **Land Group A first** (tool guardrails + /goal hooks into the loop), then Group D (lazy session DB row gate). This prevents merge conflicts within the same PR sequence and makes each commit independently revertable.

---

## 4. Testing strategy

Each commit lands with its tests:
- A1: goal continuation loop, judge fail-open, real-user-message preempt, `/goal` lifecycle (5+ tests)
- A2: loop detector hits warning then hard-stop, configurable thresholds (5+ tests, mirror hermes' `test_tool_guardrails.py`)
- A3: /steer interrupt mid-tool-call, /queue overflow behavior, ACP message ordering (~5 tests)
- A4: busy_ack toggle, footer rendering for known/unknown context_length, /footer slash (~6 tests)
- A5: OR cache HIT/MISS log, header config gate, TTL clamp (~6 tests)
- B1: SSRF guard, MIME detection, 50MB cap, capability fallback message (~8 tests, port from hermes)
- B2: Piper voice cache, lazy import path, ffmpeg conversion, missing piper-tts graceful error (~6 tests)
- B3: placeholder substitution, shell-quote-awareness, built-in-name shadowing prevention (~5 tests)
- B4: 6 channel adapters × {happy path, fallback to single-loop on error, animation peel-off where relevant} (~15 tests)
- C1: pre_gateway_dispatch action=skip|rewrite|allow, plugin crash isolation (~5 tests)
- C2: 5 surfaces from hermes (CLI once, CLI deny, gateway approve, gateway timeout, plugin crash) (~5 tests)
- C3: duration_ms is non-negative int, propagated to shell hooks, present on both hooks (~3 tests)
- D1: ghost session prune migration, lazy create, /resume picks up post-first-message session (~6 tests)
- D2: URL source claim, frontmatter name parse, --name override, locks store URL identifier (~5 tests)
- E1: consolidated vs pruned classification, REPORT.md sections, run.json schema additivity (~5 tests)

**Total expected new tests:** ~90 tests. CI must remain green; full suite must pass before push (per `feedback_full_suite_audit.md`).

---

## 5. Risks + mitigations

| Risk | Mitigation |
|------|------------|
| Lazy session creation breaks /resume picker | Migration prunes empty ghost rows; existing sessions untouched. Test: open TUI without sending → session list count unchanged after restart. |
| Tool-loop guardrails false-positive on legitimate long loops | Default thresholds match hermes (warn at 10 same-tool calls, stop at 25). Configurable via `agent.tool_guardrail_*`. |
| Goal judge auxiliary-model call adds turn latency | Judge fails OPEN → never blocks; budget=20 turns is the real backstop. Optional via `/goal pause`. |
| busy_ack default change might surprise users | Default `true` (preserves current behavior); opt-out only. |
| Piper requires ffmpeg + onnx model download | Wizard flags both; pure-text fallback if Piper init fails. |
| URL skill install ingests untrusted markdown | Trust level "community" + full security scan still runs (parity with hermes). |
| Channel `send_multiple_images` overrides may bundle differently per platform | Each adapter falls back to base loop on any error → no regression on partial failures. |
| OpenRouter cache changes pricing visibility | Header `X-OpenRouter-Cache-Status` logged; users see HIT/MISS in debug. |
| pre_gateway_dispatch fires before auth | Documented; plugins can choose to handle unauthorized senders. |
| `duration_ms` kwarg breaks existing plugins | Additive kwarg; old plugins ignore it. Default 0 if measurement fails (try/finally around `time.monotonic()` delta). |
| `/goal` auxiliary judge cost — up to 20 calls per goal (turn budget) | Use cheap auxiliary model (configurable via `agent.goal_judge_model`); fail OPEN so flaky judge doesn't wedge progress; budget cap is the hard backstop. |
| `pre_gateway_dispatch` hook fires before auth — plugin sees unauthorized senders | Documented for plugin authors; this is intentional (enables audit/handover use cases). Plugin crash is swallowed by `invoke_hook`. |
| Two PRs touching `agent/loop.py` (Group A guardrails + Group D lazy-session) clash | Land A before D within the same PR sequence; review diff at each commit boundary. |

---

## 6. Rollout

**Single PR, 4 commits in sequence:**
1. Group A — Agent core (`feat(agent): /goal + tool guardrails + /steer + /queue + busy_ack + runtime footer + OR cache`)
2. Group B — Multimodal/voice (`feat(voice,tools): video_analyze + Piper + tts command registry + send_multiple_images`)
3. Group C — Plugin platform (`feat(plugins): pre_gateway_dispatch + approval hooks + duration_ms`)
4. Group D — Storage + skills (`feat: lazy session creation + UrlSource for skills`)

Each commit is independently revertable. Tests gated per group; full suite + ruff before push.

**Branch name:** `feat/hermes-best-of-wave5`

**Worktree:** Use a dedicated git worktree (`git worktree add ../OC-wave5 feat/hermes-best-of-wave5`) per `feedback_worktrees_for_parallel_sessions.md` — never share working tree with another live session, especially with this PR's volume.

**PR title:** `feat(hermes-wave5): /goal + tool guardrails + video_analyze + Piper + 10 more (Tier 1)`

**Pre-flight check:** Before branching, run `git log origin/main --since="2 days ago" --oneline` to ensure no architectural shift on main has invalidated this scope (per `feedback_check_main_during_brainstorm.md`).

**Pre-push check:** Full pytest suite green, ruff clean, lazy-session migration tested against an existing populated `state.db`, and worktree-local CI passes — only then push (per `feedback_no_push_without_deep_testing.md`).

---

## 7. Open questions (resolved inline during plan-writing)

1. **A3 /steer + /queue scope** — hermes adds these to ACP only. Should OC also expose them in CLI/TUI like other slash commands? **Decision:** CLI/TUI parity now (small extra cost). Adds two more handler entries in `cli_ui/slash_handlers.py`.
2. **B4 channel adapters** — verified `plugin_sdk/channel_contract.py:228` has `async def send_image()`. **Decision:** add abstract `send_multiple_images()` to `channel_contract.py` with default per-image loop, then override per platform (telegram/discord/slack/mattermost/email/signal).
3. **A2 tool guardrails** — should the warning be visible to the user (TUI message) or silent in logs? **Decision:** TUI warning at warn-threshold; hard-stop at stop-threshold raises `ToolLoopGuardrailError`. Configurable thresholds via `agent.tool_guardrail_warn_at` and `agent.tool_guardrail_stop_at`.
4. **D2 URL skill source ordering** — must claim before `WellKnownSkillSource` for `/.well-known/` URLs (which routes to WellKnown). **Decision:** `UrlSource.claims(url)` returns False for `/.well-known/skills/` patterns explicitly.
5. **C2 approval hook callsite** — OC's dangerous-command gate is split across `agent/trust_ramp.py` (consent) and `agent/policy_audit.py` (audit); the actual prompt likely lives in trust_ramp. **Decision:** during implementation, locate the function that emits the y/n/once/always prompt and wrap it with the two hooks. If split across multiple sites, fire from the highest-level one.

---

## 8. Tier 2 backlog (deferred — for Wave 6)

These items are NET-NEW for OC but lower priority. Document here for future waves; do not port now.

- **Curator** (consolidated/pruned classification, per-run reports, umbrella-first prompt, cron-ticker hook) — requires building OC's curator substrate first; large enough for its own dedicated plan
- New skills: here.now (built-in), Shopify, claude-design, design-md, humanizer, airtable, comfyui-as-built-in, touchdesigner-mcp, MiniMax-AI/cli
- Trigram FTS5 index for CJK search
- `oc -z` one-shot mode
- Cron `context_from` chaining + per-job workdir + `enabled_toolsets`
- TUI: stream thinking expanded by default, light-theme auto-detection, /resume delete, archive/collapse todo panels, LaTeX rendering, mini help (`?`) — *partially in Tier 1 spillover*
- API server `POST /v1/runs/{run_id}/stop` + run status endpoint
- `/api/pty` WebSocket bridge for embedded TUI
- Session search recursive CTE for `last_active` ordering
- Dashboard: Plugins page, Models analytics page, --stop/--status flags, themes expansion
- Hardline blocklist for unrecoverable commands
- Hindsight `bank_id_template` + `HINDSIGHT_TIMEOUT`
- Kanban board (durable multi-profile collaboration) — large feature, deferred to dedicated PR
- Slack channel_skill_bindings, native slash for every gateway command
- Telegram allowlists for groups/forums
- Matrix reaction-based exec approval + dm_auto_thread
- Hermes-achievements + langfuse plugins (Hermes-branded)

## 9. Tier 3 (skipped — out of scope)

- IRC adapter (no OC user need)
- Hermes-specific dashboard polish (themes/density/layout, profile pages, reskin extension points)
- AUTHOR_MAP / release notes maintenance commits
- Docusaurus website / sidebar / docs nav (OC has its own docs structure)
- Microsoft Teams interactive setup polish (OC has its own teams adapter)

---

## 10. Success criteria

- All 14 Tier 1 features merged on `main` via single PR
- 85+ new tests, full suite green, ruff clean
- No regression on existing 8,800+ tests
- PR description maps each item to its hermes commit hash + new OC file paths
- Memory entry recorded after merge in `MEMORY.md` (`project_hermes_wave5_done.md`)
