# Gateway-vs-CLI intelligence parity — Plan

Date: 2026-05-17
Owner: Saksham
Working dir: `/Users/saksham/Vscode/claude/OpenComputer`
Scope: ALL 18 channel adapters (telegram, discord, slack, whatsapp, signal, matrix, mattermost, feishu, wecom, weixin, dingtalk, qqbot, imessage, irc, sms, email, homeassistant, webhook). Verified — they all route through `Dispatch.handle_message` → `loop.run_conversation` and outgoing replies go through `outgoing_drainer.truncate_smart`.

Supersedes: `docs/superpowers/specs/2026-05-17-gateway-vs-cli-intelligence-gap/ANALYSIS.md` (diagnosis-only; this is the proper Senior Engineer Workflow plan).

---

## Pre-work — what I'm sure of (greped, not guessed)

| Claim | Evidence |
|---|---|
| 18 channel adapters all hit one dispatcher | `extensions/{telegram,discord,...}/adapter.py` all call `self.handle_message(event)` |
| Gateway path filters tools via `allowed_tools` | `gateway/agent_loop_factory.py:108-117` |
| CLI path never sets `allowed_tools` (wildcard) | `cli.py:1819` — `AgentLoop(provider=provider, config=cfg, compaction_disabled=no_compact)`, no allowed_tools kwarg |
| Routing rules silently substitute the whole system prompt | `loop.py:2079-2090` — comment literally says "declarative / skills / memory / SOUL injection OFF" |
| Truncation fires for ALL platforms, not just Telegram | `outgoing_drainer.py:138-144` — `truncate_smart(body, max_len=cap)` with per-platform cap |
| `runtime_footer` defaults to OFF | `runtime_footer.py:38-40` — `FooterConfig.enabled: bool = False` |
| 18 adapters confirmed | `ls extensions/` filtered: dingtalk, discord, email, feishu, homeassistant, imessage, irc, matrix, mattermost, qqbot, signal, slack, sms, telegram, wecom, weixin, whatsapp + webhook |
| `agent_context` stays `"chat"` on gateway path | Greped — no `agent_context=` assignment in `gateway/dispatch.py` |

### Why this isn't just a Telegram problem

The user's question said "Telegram or Discord or whatever." That intuition is correct — the asymmetry is **at the dispatcher layer**, not in any adapter. Every adapter inherits the same 10 mechanisms because they all use the same gateway plumbing. **Fixes here ship to all 18 platforms simultaneously.** Negative restatement: a fix that only helps Telegram is misdesigned.

---

## Honest self-audit of the previous ANALYSIS.md

Before writing this plan, I'm grading the prior file:

| Aspect | Grade | Notes |
|---|---|---|
| Diagnostic accuracy | A | Each of the 10 mechanisms is grepped + cited |
| Plan quality | D | "Tier 1/2/3" was gut-feel scoring; no /brainstorm; no /audit; punted on decision via A/B/C/D options |
| Calibration | B | Severity ranking was vibes, not measured impact |
| Honesty about scope | C | Implied "10 separately fixable" without owning that fixing all 10 is months of unscoped work |
| Practical value to next session | C+ | Useful as diagnosis; gives Claude Code a bad map for execution |

So this plan **replaces** that doc's "fixes" section. The diagnosis stays valid as background reading.

---

## Phase 1 — /brainstorm

### Goal

Close the perceived intelligence gap between CLI and gateway sessions across all 18 channel adapters, with bounded scope (must ship in <8 weeks) and without breaking existing routing/profile semantics that some users may depend on.

### Constraints

- **Must apply to all 18 adapters.** Per-adapter patches forbidden.
- **No regression for users who deliberately use routing/profile rebinding** (e.g. `@stocks_bot` Telegram template). That's a feature.
- **No regression for the consent gate.** Gateway can't have full CLI tool surface without consent enforcement intact.
- **Backwards-compat the config schema.** Existing `bindings.yaml` + `routing` blocks must keep working unchanged.

### Approaches considered

#### Approach A — "Make gateway use CLI's exact construction"

Strip `allowed_tools` from `agent_loop_factory.py`. Stop calling `truncate_smart`. Disable routing's `system_prompt_override`. Effectively: gateway becomes "CLI but with a chat adapter."

- **Effort:** S (3-4 days).
- **Risk:** Very high. Breaks every user who configured per-channel routing/templates. Breaks platform compliance (Telegram rejects >4096-char messages → silent failure). Removes the security gate that filters tools per profile.
- **Upside:** Total parity, zero asymmetry.
- **Verdict:** Reckless. Rejected on merit — would break existing deployments.

#### Approach B — "Per-mechanism feature flags"

Add 10 config flags, one per mechanism (`gateway.disable_prompt_override`, `gateway.disable_tool_filter`, `gateway.disable_truncation`, …). Default each to OFF (current behavior). User opts into parity per-feature.

- **Effort:** L (4-5 weeks).
- **Risk:** Medium. Config bloat. 10 new knobs is a maintenance nightmare. Users won't know which to enable.
- **Upside:** Maximum control. Power users get exactly what they want.
- **Verdict:** Punt disguised as engineering. Rejected — too many knobs.

#### Approach C — "One `gateway.parity_mode` config flag"

Single boolean: `gateway.parity_mode: true` makes ALL 10 mechanisms behave like CLI for gateway sessions. Default OFF (back-compat). When ON: no tool filter, no prompt override (PromptBuilder always wins), no truncation (chunk-and-send instead), runtime_footer enabled, etc.

- **Effort:** M (3-4 weeks).
- **Risk:** Medium. One big switch is opaque — users don't know what changed when they flip it. Also some mechanisms (truncation) genuinely need to happen per-platform; can't just disable.
- **Upside:** Discoverable. Documented as "make my Telegram feel like my CLI."
- **Verdict:** Better than B, worse than D. Hides per-mechanism trade-offs.

#### Approach D — "Fix the 3 highest-impact mechanisms, document the rest as deferred"

Pick the 3 mechanisms with the biggest combined impact, fix them properly (with tests + docs + migration), defer the other 7 with a documented rationale. Pareto principle.

- **Effort:** M (3-4 weeks).
- **Risk:** Low. Each fix is scoped and tested. Deferred items don't pretend to be in scope.
- **Upside:** Predictable delivery. Honest scope.
- **Verdict:** Strong candidate.

#### Approach E — "Observability-first: ship the diagnostic surface, fix later"

Don't fix anything functionally. Ship `runtime_footer` enabled by default + `oc gateway diagnose` CLI + `agent.log` audit table showing which mechanisms fired on each turn. Users *see* the asymmetry and can choose to fix per-profile via existing levers.

- **Effort:** S (1-2 weeks).
- **Risk:** Low.
- **Upside:** Users self-serve. No code changes to the agent loop. Buys time to learn which mechanisms actually matter in practice.
- **Verdict:** Necessary but not sufficient. Doesn't close the gap, only surfaces it.

#### Approach F — "Deprecate routing/profile rebinding entirely; one gateway = one agent"

Aggressive: remove the `routing` and `bindings` features. All gateway sessions use the same profile + config the CLI uses. Per-channel customization is done via skills + tools, not at the prompt-override layer.

- **Effort:** L (6-8 weeks including migration of existing users).
- **Risk:** Very high. Real users have deployed per-channel templates.
- **Upside:** Architecturally clean. No asymmetry to close because the asymmetry-creating subsystems are gone.
- **Verdict:** Rejected. Throws away real user value.

#### Approach G — "Compose D + E: ship observability now, then fix top-3 once we have data"

Two-track. Track 1 (1-2 weeks): runtime_footer on + diagnostic CLI + per-turn log of which mechanisms fired. Track 2 (3-4 weeks, after 2 weeks of telemetry collection): fix the top-3 mechanisms based on what telemetry shows is actually firing most.

- **Effort:** L (5-6 weeks total, sequential).
- **Risk:** Low.
- **Upside:** Data-driven prioritization. Track 1 also independently valuable.
- **Verdict:** Best risk-adjusted upside.

#### Approach H — "Unify the construction path"

Refactor: both `cli.py::_cmd_chat` and `gateway/agent_loop_factory.py` call a single `build_agent_loop(profile, source)` function. Source is `"cli" | "gateway"`. The function applies source-specific defaults (e.g. gateway gets `allowed_tools` filter, CLI doesn't) but the asymmetries become explicit + auditable in one place instead of scattered. Doesn't fix anything per se; makes future fixes much cheaper.

- **Effort:** M (2-3 weeks).
- **Risk:** Medium. Refactoring two call sites is straightforward; the surface area inside `_cmd_chat` is huge (~60 LOC of one-off setup).
- **Upside:** Foundational. All future parity work cheaper. Makes the asymmetry an explicit code surface instead of a tribal-knowledge gotcha.
- **Verdict:** Strong candidate; pairs well with D or G.

### Scoring

| Approach | Effort | Risk | Upside | Verdict |
|---|---|---|---|---|
| A — Make gateway = CLI literally | S | Very High | High (if it didn't break things) | Reckless |
| B — 10 feature flags | L | Medium | Medium (config bloat) | Engineering punt |
| C — One parity_mode flag | M | Medium | Medium-High (opaque) | OK, second-best |
| D — Fix top 3, defer 7 | M | Low | High (pareto) | Strong |
| E — Observability-first | S | Low | Medium (surfaces gap) | Necessary, not sufficient |
| F — Deprecate routing | L | Very High | High (clean arch) | Throws away value |
| G — E then D, data-driven | L (sequential) | Low | **Highest** | **Winner** |
| H — Unify construction | M | Medium | Foundational (cheaper future) | Pairs with D or G |

### Convergence

Top 3: **G, D, H.**

- **D** alone fixes the gap but picks "top 3" by intuition (today). What if telemetry shows mechanism #5 fires 80% of the time?
- **G** adds 2 weeks of telemetry first, then picks top-3 by data. Stronger.
- **H** is foundational; doesn't directly close the gap but makes future fixes 2-3x cheaper. Pairs with G as the "where the fixes land."

### Winner: **G + H combined**

Why G+H beats G alone and D alone on merit:

- **G's data-driven prioritization** avoids the "fix what I think matters" trap. The telemetry phase costs 2 weeks; that's an honest investment for a 3-4 week fix phase.
- **H provides the seam** where the fixes land. Without H, fixing 3 mechanisms = 3 patches in 3 different files. With H, it's one function with 3 conditional branches that are testable in isolation.
- **G alone has a refactoring shaped hole** where the fixes go. H fills it.
- **D alone bets on intuition.** Today my intuition says mechanisms 1, 3, 6 dominate (prompt override, truncation, profile rebind). Telemetry might say 2, 4, 7 (tool filter, channel overlay, persona). If I'm wrong and we shipped D, we wasted 3-4 weeks fixing the wrong things.

What loses on merit:
- **A** is reckless.
- **B** is config bloat.
- **F** throws away real user value (routing is genuinely useful for `@stocks_bot`-style bots).
- **C** hides per-mechanism trade-offs in a single flag.

---

## Phase 2 — /audit-design

Stress-testing G+H.

### 1 — Assumption check

| Assumption | Status | Resolution |
|---|---|---|
| Telemetry will produce actionable data within 2 weeks | **Unvalidated.** Depends on Saksham's gateway usage volume. If he sends 3 Telegram messages in 2 weeks, n=3 isn't actionable. | Pre-Track-1 task: estimate gateway message volume from `audit.db`. If <50 messages/week across all platforms combined, extend the data-collection phase or skip telemetry. |
| The 10 mechanisms I identified are the right 10 | **Mostly validated** (each grepped + cited). | Track 1's telemetry can surface mechanisms I missed (e.g. a hook that fires only on gateway). Accept that "top 3" may include something not on the current list. |
| `outgoing_drainer.truncate_smart` actually triggers on every gateway reply | **Validated** (`outgoing_drainer.py:138-144`). | — |
| All 18 adapters use `Dispatch.handle_message` | **Validated** (grep on `extensions/*/adapter.py`). | — |
| The refactor (H) is contained — won't touch the agent loop's internals | **Unvalidated.** `cli.py:1819` is one line, but the surrounding setup (provider resolution, plugin loading, ContextVars) is ~60 LOC. Pulling that into a shared function may pull in unwanted coupling. | Track H1: read the full setup paths on both sides before estimating. If the shared surface is too big, fall back to H-lite (extract only the `AgentLoop(...)` constructor call; leave surrounding setup duplicated). |
| Users won't break when runtime_footer turns on by default | **Unvalidated.** Some bot deployments may parse the assistant's reply for downstream automation (e.g. trigger a workflow on a keyword). A "model: claude-opus" footer could confuse those. | Mitigation: turn footer on by default ONLY for new installs; existing users see no change. Add `oc gateway footer enable` for opt-in. |

### 2 — Architecture stress (edge cases)

- **Adapter X sends a 10,000-character reply.** Per-platform cap kicks in. With Track 2's fix-truncation change, agent sees the full reply was chunked into 3 messages. Agent's next turn references "what I just sent you" — needs to remember the full body, not the chunked surface. Resolution: chunk on the outgoing side ONLY; preserve the full reply in `messages` table so context_pruning/compaction sees the whole thing.

- **User runs gateway on a 1990s VPS, audit.db logging adds latency.** Track 1's telemetry is just structured JSON lines appended to audit.db (already exists). Worst case: ~1ms per turn. Acceptable.

- **Two adapters fire concurrently for the same chat.** Already handled by the per-chat lock in `dispatch.py`. No new race introduced.

- **Routing rule matches but `system_prompt_override` is the empty string.** Currently this would wipe the PromptBuilder and inject empty. With Track 2's "merge_with_builder" fix, empty override = no-op. Resolution: empty-string treated as None.

- **Cron-triggered messages.** Cron uses `agent_context="cron"`, takes a separate code path. Doesn't go through gateway. Verified — `cron/scheduler.py` calls AgentLoop directly. No changes needed in cron.

- **User has `runtime_footer.fields = []`.** Footer enabled but empty — renders blank suffix. Resolution: if fields list is empty, treat as disabled regardless of `enabled: true`.

- **User has parity-mode-style customization via plugin.** A plugin might already inject custom truncation/prompt override behavior. Resolution: H's unified `build_agent_loop` exposes hooks so plugins can override defaults; doesn't break existing plugin contracts.

### 3 — Alternative dismissal

| Approach | Dismissed because |
|---|---|
| A | Removes routing entirely — breaks `@stocks_bot` users. Reckless. |
| B | 10 flags = engineering laziness disguised as choice. |
| C | One opaque flag hides per-mechanism trade-offs. Worse than D. |
| F | Throws away the `routing` feature that real users deploy. |

D and E both become **components of G** (D is Track 2; E is Track 1). H pairs with G.

### 4 — Requirement gap

- **The user wants the Telegram session to *feel* as smart as the CLI session.** Implicit: behavioural change must be observable from the chat, not just from logs. Track 2's fixes (chunked replies, full prompt, real tool surface) deliver felt improvement. Track 1's telemetry is invisible to end user.

- **The user wants this to apply to ALL adapters, not just Telegram.** Explicit in the question. Confirmed by greping — fixes at the dispatcher layer hit all 18.

- **Implicit: don't surprise current users.** Per-channel templates (e.g. `stocks_bot`) must keep working unchanged. Track 2 fixes must be opt-in by default OR transparently preserve old behaviour for explicit-template users.

- **Implicit: maintainability.** Future-OC devs reading `gateway/CLAUDE.md` should find the asymmetry documented. Track 1 includes doc updates.

### 5 — Composability

- **Track 1 + Track 2:** telemetry collected during Track 1 informs Track 2's prioritization. Clean handoff. ✓
- **G + H:** H ships during Track 2 as the seam where fixes land. Doesn't block Track 1. ✓
- **Track 2 fixes + existing routing/templates:** opt-in flags on routing rules let template authors say "this template wants to merge with PromptBuilder, not replace it." Backwards-compat preserved. ✓
- **Truncation chunking + outgoing_queue:** chunking happens before queueing; queue treats each chunk as a separate message. Per-chat ordering preserved (queue is FIFO per chat_id). ✓

One real composability risk: **runtime_footer's "model: X · context: Y%" suffix could trigger consent prompts on platforms that scan messages for keywords**. Resolution: footer is appended after consent prompt logic runs. Document the ordering.

### 6 — Scope honesty

Where I'm tempted to undersize:

- **Track 1 telemetry "1-2 weeks"** assumes I can wire 10 structured-log emit points into `dispatch.py` cleanly. Realistic: 2 weeks including tests and the diagnostic CLI. Plus 1-2 weeks of letting telemetry accumulate before drawing conclusions. **Honest: 3-4 weeks for Track 1.**

- **Track 2 "fix top-3 mechanisms"** sounds bounded. Each mechanism has its own depth:
  - Fix #1 (prompt override): needs `merge_with_builder` field added to routing schema + template loading + tests + docs + migration. **M (1 week).**
  - Fix #3 (truncation): chunking-with-ordering across all platforms + tests on 18 adapters' send paths. **L (2 weeks).**
  - Fix #6 (profile rebind silence): badge in chat + opt-in surfaces. **S (3-4 days).**
  - **Honest Track 2: 3-4 weeks.**

- **H (unified construction):** reading both call sites + extracting + testing without regression. **2-3 weeks.**

**Total honest: 8-10 weeks for one engineer, sequential.** That's longer than the 8-week constraint allows. Need to cut.

### 7 — API stability

- New: `RoutingRule.merge_with_builder: bool = False` field on routing template schema. **v1 API commitment.** Additive, default False (current behaviour). Existing rules unaffected.
- New: `display.runtime_footer.enabled` defaults change for new installs. **NOT an API change** (config defaults aren't API).
- New: `oc gateway diagnose` CLI command. **User-facing API.** Lock flag names.
- New: `build_agent_loop(profile, source)` helper. **Internal.** Not in plugin_sdk.
- New: outgoing chunking changes message-count semantics. Telegram bot frameworks parsing "messages sent" stats may see the count go up. **Document as a behaviour change.**

### 8 — Failure map

| Choice | Production failure | Mitigation |
|---|---|---|
| Track 1 telemetry adds latency | Slower gateway responses | Async append to audit.db; off-hot-path |
| Track 2 chunked-reply changes message count | Bot frameworks counting messages break | Document; provide `gateway.chunk_replies: false` for opt-out |
| `merge_with_builder=True` template confuses model with conflicting instructions | Worse responses, not better | Default False; flag in docs that "additive prompts may conflict" |
| Refactor (H) silently regresses CLI behaviour | CLI users notice flaky model swaps | Pre-refactor: capture snapshot of CLI behavior in a smoke test; gate refactor on identical output |
| runtime_footer enabled by default breaks bot keyword-scanners | Webhook automations fire incorrectly | Enable only for fresh installs; opt-in for existing |
| Telemetry data is too sparse (n<50/week) to prioritize | Track 2 falls back to gut-feel = same as Approach D | Acknowledged in §1; if low volume, run synthetic load |

### 9 — YAGNI sweep

- **Per-tool latency telemetry.** No — focus on which mechanisms fire, not per-tool.
- **GUI for parity-mode toggles.** No — config file is the interface.
- **Per-adapter telemetry breakdown.** Yes-but-cheap — `platform` is already in the log row, breakdown is just SQL.
- **Custom truncation strategies per-platform.** No — one good chunking algo for all platforms.
- **Backporting fixes to oc-workspace web UI.** No — workspace already uses the wire directly, not the gateway's truncation path. Separate concern.
- **`gateway.parity_mode: true` one-flag fallback.** Considered. If after Track 2 ships, users still want a single switch — add later. v1 ships per-mechanism.

After YAGNI cuts: total = **8-9 weeks sequential**. Still over budget. Real cuts needed.

### What to cut from G+H to fit 6-8 weeks

Honest options:

1. **Cut H (unified construction).** Saves 2-3 weeks. Track 2 fixes go in 3 separate places. More maintenance burden long-term but ships faster.
2. **Cut Track 1 (telemetry).** Saves 3-4 weeks. Fall back to D (gut-feel top 3). Faster delivery but worse prioritization.
3. **Reduce Track 2 to top 2 instead of top 3.** Saves 1 week.

**Recommendation: cut H to a v2 follow-up.** It's foundational but not user-facing. Saves 2-3 weeks. Track 1 + Track 2 with top 3 fixes lands in 7 weeks total.

Revised total: **6-7 weeks sequential.** Fits budget.

---

## Phase 3 — /plan

Revised plan: **G (without H for v1).** Track 1 (telemetry + observability) ships first, Track 2 (top 3 fixes) ships after 2 weeks of telemetry.

### "Done" in one sentence

For every gateway session across all 18 channel adapters, the user can (a) see which agent profile + model + tools answered each turn via an opt-in footer, (b) inspect via `oc gateway diagnose` which of the 10 parity-affecting mechanisms fired on the last N turns, and (c) the top-3-by-telemetry mechanisms have been fixed so gateway responses match CLI quality within ±15% on a defined benchmark prompt set.

### Milestones

#### Milestone 1 — Observability surface (LOAD-BEARING, **MVP**)

Done when: every gateway turn logs which of the 10 mechanisms fired into `audit.db.gateway_parity_log` table; `oc gateway diagnose --session <id>` shows a Rich table of the last N turns and which mechanisms tripped; `runtime_footer` enable is documented + 1-line config change works.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T1.1 Volume audit: run `sqlite3 audit.db "SELECT COUNT(*) FROM ... WHERE platform != 'cli'"` to estimate weekly gateway turn count. If <50/week, escalate (telemetry won't be actionable). | XS | — | If low volume, decide whether to proceed (synthetic load) or skip Track 1 |
| T1.2 Design `gateway_parity_log` schema: `(session_id, turn_id, platform, ts, mechanism_id, fired:bool, detail:json)`. Mechanisms 1-10 are enum values. | S | T1.1 | Schema lock — additive only |
| T1.3 Instrument `dispatch.py::handle_message` + `_do_dispatch` to emit one log row per mechanism per turn (fired or not). 10 emit points total. | M | T1.2 | Code surface is dense; risk of missing edge paths |
| T1.4 Instrument `outgoing_drainer.py::truncate_smart` to emit truncation events (mechanism #3). | XS | T1.2 | — |
| T1.5 Instrument `loop.py:2079-2090` to emit when `system_prompt_override` wipes the builder (mechanism #1). | S | T1.2 | — |
| T1.6 `oc gateway diagnose --session <id>` CLI command. Mirrors `oc consent list` shape. | M | T1.3 | UX: must be readable |
| T1.7 `oc gateway diagnose --rollup --since 7d` aggregate view: which mechanisms fired most? | S | T1.6 | — |
| T1.8 `runtime_footer` enabled-by-default for fresh installs; opt-in for existing. Setup-wizard prompts new users. | S | — (parallel) | Doc the change clearly |
| T1.9 Tests: `tests/gateway/test_parity_log_emit.py` (each mechanism emits when expected), `tests/cli/test_gateway_diagnose.py` (golden output) | M | T1.6 | — |
| T1.10 Docs: `docs/gateway/intelligence-parity.md` linking the diagnosis (`ANALYSIS.md`) + this plan + the CLI command | S | T1.6 | — |

Milestone-1 total: ~**L** (10-14 working days = 2-3 weeks).

#### Milestone 2 — Telemetry collection window (1 calendar week, low effort)

Done when: 1 week of gateway usage has accumulated in `gateway_parity_log`; the user (or auto-rollup script) has identified the top-3 most-firing mechanisms.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T2.1 Pause; let telemetry accumulate. Saksham keeps using gateway sessions normally. | — | M1 done | Calendar gate, not engineering |
| T2.2 At end of week, run `oc gateway diagnose --rollup --since 7d` and identify top-3 mechanisms by firing frequency × estimated impact (severity ranking from ANALYSIS.md). | S | T2.1 | Saksham's call which 3 to fix |
| T2.3 Update PLAN.md (this file) to lock the top-3 list. Until done, M3 tasks are intentionally vague. | XS | T2.2 | — |

Milestone-2 total: **1 week** (calendar; no code).

#### Milestone 3 — Fix top-3 mechanisms (data-driven)

Done when: telemetry-identified top-3 mechanisms are fixed with tests + docs + opt-in/opt-out story; behavioral parity benchmark shows gateway responses ≥85% as good as CLI on a defined prompt set.

This milestone's tasks are **filled in at the end of M2** based on actual data. I'm pre-listing what each of the 10 mechanisms' fix would look like, so M3 task list snaps to top-3 once chosen:

| Mechanism | Fix | Size |
|---|---|---|
| #1 prompt override wipes builder | Add `merge_with_builder: bool = False` to `ResolvedTemplate`; `loop.py:2085` merges instead of replaces when True | M (1 week) |
| #2 tool allowlist | Add `gateway.tool_filter: strict|wildcard|profile` config (default `profile` = current behavior; `wildcard` = match CLI) | S (3 days) |
| #3 reply truncation | Chunk-and-send with `(1/N) (2/N)` markers; preserve full body in messages DB | L (2 weeks) |
| #4 channel prompt overlay | Same fix as #1 but in `_build_channel_runtime` path (`dispatch.py:1596-1622`) | S (3-4 days) |
| #5 no interactive consent | Async consent: post approval msg + proceed when reply lands. Multi-tool turn doesn't serialize. | L (2 weeks) |
| #6 profile rebind silence | Inject a one-line badge into first reply of a rebind-triggered session | S (3 days) |
| #7 persona casual register | `display.persona_override` config to force `task` mode on gateway | S (2 days) |
| #8 routing-decision invisibility | Inject "[routed to: stocks_bot]" badge when routing fires | XS (1 day) |
| #9 runtime_footer off by default | Already in M1 (T1.8) | — |
| #10 long-session compaction | Out of scope (would need session-fork-aware compaction; separate spec) | XL — deferred |

If telemetry shows {1, 3, 6} as top-3: ~**4 weeks** (1+2+0.6).
If {1, 4, 8}: ~**1.5 weeks** (lucky).
If {3, 5, 10}: **6+ weeks** (worst case, including the deferred #10).

| Task | Size | Deps | Risks |
|---|---|---|---|
| T3.1-T3.N Fix tasks for the chosen top-3, derived from M2's output | M-L (varies) | M2 done | Scope depends on which 3 are chosen — accept |
| T3.X Behavioral parity benchmark: 20 standard prompts run through CLI + gateway-via-each-platform; record assistant response quality (length, tool-use count, accuracy on factual prompts). Target: ≥85% parity. | M | M3 fixes done | Subjective scoring — define rubric upfront |
| T3.Y Tests for each fix | M | T3.1-T3.N | — |
| T3.Z Docs: update `gateway/CLAUDE.md` with what changed | S | T3.Y | — |

Milestone-3 total: **2-6 weeks** depending on top-3 choice. Plan with 4 weeks as the expected case.

#### Milestone 4 — Document the rest as deferred

Done when: every non-top-3 mechanism has a one-line entry in `docs/gateway/deferred-parity-work.md` explaining why it's deferred and what its fix would look like. Honest scope.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T4.1 List remaining 7 mechanisms + fix sketches | S | M3 done | — |
| T4.2 Cross-link from `gateway/CLAUDE.md` and SESSION-HANDOFF.md | XS | T4.1 | — |

Milestone-4 total: **1-2 days**.

### Milestone summary

| # | Milestone | Size | Calendar |
|---|---|---|---|
| **1 (MVP)** | **Observability surface (telemetry + diagnose CLI + footer)** | L | 2-3 weeks |
| 2 | Telemetry collection window | (gate) | 1 week |
| 3 | Fix top-3 mechanisms (data-driven) | L | 2-6 weeks (expected 4) |
| 4 | Document the rest as deferred | XS | 1-2 days |

**Total expected calendar: 6-9 weeks for one engineer, sequential.**

### Explicitly out of scope

- Per-platform fixes (anything that only helps Telegram, not the other 17 adapters).
- Web UI / dashboard changes — separate concern; workspace bypasses the gateway truncation path.
- Cron-path changes — different code path, different RuntimeContext, not asymmetric in the same way.
- The remaining 7 mechanisms after M3 picks top 3 — deferred with docs.
- H (unified construction refactor) — deferred to v2; would save 2-3 weeks total cost if done now but blocks the v1 timeline.

---

## Phase 4 — /audit-plan

Harsh critic pass.

### 4.1 — Unvalidated assumptions

| Assumption | Status | Plan revision |
|---|---|---|
| Saksham's gateway volume produces actionable telemetry in 1 week | T1.1 explicitly checks; if low, escalate | If low volume: option A (extend window to 4 weeks) or option B (skip telemetry, fall back to D) |
| Top-3-by-firing-frequency is the right ranking | Should be firing × severity, not pure firing. Persona overlay fires every turn but is low severity. | T2.2 multiplies firing-frequency × ANALYSIS.md severity weight |
| Behavioral parity is measurable on a 20-prompt benchmark | Untested. CLI and gateway have different temperatures, contexts, history. ±15% may be noise. | Pre-M3: pilot the benchmark on 5 prompts; if variance >20% even within CLI runs, benchmark is unreliable; cut the parity claim from the "Done" criterion |
| `outgoing_drainer.truncate_smart` is the only outgoing-side modifier | Likely yes (greped — only one truncate caller). If others exist, the chunking fix won't catch them. | T1.4 includes a search for any other transformer in `outgoing_drainer.py` |
| Routing's `system_prompt_override` is the only path that wipes PromptBuilder | Channel overlay does the same. Both must be fixed if mechanism #1 wins. | T3 lists fix #1 and fix #4 as a pair; if #1 lands in top-3, #4 piggybacks |

### 4.2 — Undersized tasks hiding real complexity

- **T1.3 "Instrument 10 emit points"** sounds bounded. Reality: the 10 mechanisms live in 4 different files (`dispatch.py`, `outgoing_drainer.py`, `loop.py`, `runtime_footer.py`). Each emit point needs a try/except wrapper, a stable event-shape, and tests. Honest size: **M-L (1.5 weeks)**, not M.
- **T2.2 "Saksham picks top 3"** isn't engineering. It's a 1-hour conversation. But the chosen 3 dramatically affect M3's scope. If Saksham picks {#3, #5, #10}, M3 balloons to 6+ weeks. **Accept the variance; document the expected case (4 weeks) and the worst case (6+).**
- **T3 fixes** size table is honest but I haven't audited the dependencies between them. Fix #4 (channel overlay) depends on fix #1 (prompt-override merge) — if #1 isn't in top-3, #4 alone can still ship via its own merge flag. Already noted but worth re-flagging.
- **Behavioral parity benchmark (T3.X)** is a 1-week task disguised as a checkbox. Designing 20 prompts that work on both CLI and gateway, scoring rubric, running them across N platforms = real work. **Honest size: M (1 week).**

After resizing: M1 grows from 2-3 weeks to **3-4 weeks**. M3 expected case grows from 4 to **5 weeks** (including benchmark). **Total expected: 8-10 weeks.** That's the honest upper bound.

### 4.3 — What breaks if Milestone 1 slips

M1 is the MVP. If it slips:

- **M2 can't start** without telemetry.
- **M3 fixes can ship without telemetry**, falling back to gut-feel prioritization (Approach D). Saves M2's calendar gate (1 week) and pushes M3 to start sooner, but loses the data-driven prioritization that justifies G over D.
- **runtime_footer can ship independently** of the telemetry — it's M1's T1.8 and isn't actually gated on T1.1-T1.7. **Ship T1.8 as a single fast-track PR first.** User gets immediate visibility into which model answered.

**Mitigation:** carve T1.8 out as a stand-alone "1-day fast-track" that lands week 1, decoupled from the rest of M1.

### 4.4 — Simpler path to the same outcome?

**Considered: skip Track 1 entirely, just fix top-3 by my gut-feel (Approach D).** Saves 3-4 weeks.

**Rejected.** My gut-feel ranking in ANALYSIS.md (1 > 3 > 6) is just a hypothesis. If Saksham's actual usage shows mechanism #2 (tool filter) fires on 90% of turns and produces the worst gaps, my fix list misses the mark. 3-4 weeks of telemetry beats 3-4 weeks of fixing the wrong things.

**Considered: ship runtime_footer ONLY** (T1.8 from M1) and let users self-diagnose. **No further code.**

**Rejected as the v1 solution; accepted as the week-1 fast-track within M1.** The footer alone surfaces the problem but doesn't fix it. Many users won't notice the footer or won't know what to do with the info.

**Considered: replace M2 with synthetic load.** Run a script that fires 100 gateway messages through each of the 18 platforms; collect telemetry instantly.

**Partially accepted.** If T1.1 reveals real volume is <50/week, fall back to a synthetic-load script. Add as M2.alt task.

### 4.5 — Pre-emptive retro

1. **"Telemetry took 4 weeks to accumulate, not 1, because my real gateway volume is low."** → Synthetic load fallback is documented in §4.4. Trigger if T1.1 shows low volume.
2. **"M3 picked fixes #3 + #5 = 4-week burst that overran."** → Plan acknowledges 2-6 week range. Document expected case (4 weeks) AND worst case (6+) up front so no surprise.
3. **"runtime_footer enable broke a webhook automation that scanned replies."** → Mitigation: opt-in for existing installs. Documented in §8 failure map.
4. **"The behavioral parity benchmark scored 60% — fixes didn't actually close the gap."** → Then telemetry was right but the fixes themselves were wrong. Acceptable failure mode; we ship Track 1 anyway as standalone value, and re-spec Track 2.
5. **"We should have done H first; the 3 fixes ended up scattered across the same dispatcher file and conflicted on merges."** → Accept. H is documented as v2; if M3 reveals real conflict pain, H accelerates before v1 ships.

All 5 folded into the plan above.

### 4.6 — Revised plan summary

The plan that ships, after audit:

1. **Week 1 (fast-track):** Ship runtime_footer enabled-by-default for fresh installs (T1.8 alone). Single PR.
2. **Weeks 1-4 (M1):** Build observability — instrument all 10 mechanisms in `audit.db`, ship `oc gateway diagnose`, write docs. Honest size 3-4 weeks (was 2-3).
3. **Week 5 (M2):** Telemetry collection window. If real volume <50/week, run synthetic load instead.
4. **Weeks 5-10 (M3):** Fix top-3 mechanisms identified by telemetry. Expected case 4 weeks; worst case 6.
5. **Week 10 (M4):** Document deferred 7 mechanisms in `docs/gateway/deferred-parity-work.md`. 1-2 days.

**Total expected calendar: 8-10 weeks for one engineer.**

**Explicit deferrals:**
- H (unified construction refactor) → v2, would save 2-3 weeks of future fix cost but blocks v1 timeline
- Top-7-non-chosen mechanisms → documented in M4
- Web UI / dashboard parity work → separate concern
- Cron-path parity → different architecture, not asymmetric in this way
- "One parity_mode flag" (Approach C) → consider only if users explicitly ask after v1

### 4.7 — Pre-flight checklist before any code

- [ ] Confirm `audit.db` is writable + has schema migration support.
- [ ] Run T1.1 (volume audit) before estimating M2's calendar gate.
- [ ] Confirm parity plan (Hermes parity / awareness cleanup / TUI parity) is NOT executing concurrently. This work touches `gateway/dispatch.py` heavily; merge conflicts will hurt.
- [ ] Backup current `bindings.yaml` + `routing` config — Track 2's fixes change template-load semantics.
- [ ] Confirm `pytest opencomputer/gateway/` is green.

If any fail: halt and report.

---

## Honest closing note

This plan replaces the previous ANALYSIS.md's "Tier 1/2/3 fixes" with a proper data-driven design. **The previous fix list was gut-feel.** This one buys 4-5 weeks of telemetry-collection before committing to which fixes to ship. That's an honest investment.

**Three caveats worth your call:**

1. **Total calendar is 8-10 weeks**, longer than the soft 8-week constraint in §"Constraints." If you want to fit 8 weeks hard, cut M3 from "fix top-3" to "fix top-2" (saves ~1 week).

2. **The "behavioral parity benchmark" (T3.X) is the riskiest part of M3.** Defining "is gateway as smart as CLI?" objectively requires a prompt set + scoring rubric that doesn't exist yet. If you'd rather drop the benchmark and ship fixes on developer judgment, M3 shrinks by 1 week.

3. **The 10 mechanisms apply to all 18 platforms, but some platforms have additional asymmetries** (e.g. Telegram's 4096-char limit vs Matrix's 65535-char limit interact with mechanism #3). v1 fixes use the most-restrictive cap. Per-platform tuning is post-v1.

Recommendation: ship M1 (with the T1.8 fast-track in week 1), let telemetry run during M2, then decide on M3 scope. Don't commit to M3's full 6 weeks until you've seen what telemetry says.
