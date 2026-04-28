# Hermes Agent vs OpenComputer — Deep Major-Gap Audit

**Date:** 2026-04-28
**Scope:** Every meaningful capability Hermes Agent ships, mapped to OpenComputer's current state, ranked by strategic impact so the next port wave can target the highest-leverage gaps first.
**Companion docs:**
- `docs/refs/hermes-agent/inventory.md` (2026-04-22) — original feature-by-feature mapping with verdicts
- `docs/superpowers/specs/2026-04-28-hermes-channel-feature-port-design.md` — approved channel-port plan (~15.5 dev-days, 6 PRs)
- `docs/audit/05-skills-tools-plugins.md` (2026-04-27) — structural audit of OC's three extension surfaces
- `docs/audit/06-privacy.md` (2026-04-27) — egress + redaction audit with 17-entry risk register
- `docs/superpowers/specs/2026-04-27-platform-reach-port-guide.md` — Track A (shared infra) / Track B (per-channel adapter) port guide
- **OpenClaw companion gap doc:** to be written next session on user signal

**Ground truth as of writing:** OC main `5c62a12` plus April 26-28 squashes through PR #219 (passive-education v2). 7 bundled extensions in `extensions/` (per CLAUDE.md) plus 9 channel adapters from Track-B already-shipped (memory `project_track_b_already_shipped.md`) plus voice-mode (#199), browser-control (#202), T2 batch skills (#203), auto-skill-evolution (#193/#204), ambient-foreground-sensor (#184), layered-awareness V2.B/V2.C (#155/#163), TUI Phase 1+2 (#180/#200), OI removal → native introspection (#179), and 28-item parity block from PR #171 (Tier S Hermes port).

> **How to read this doc:** Tiers go from "ship-shaping strategic" (Tier 1) → "polished UX wins" (Tier 2) → "operational hardening" (Tier 3) → "plugin ecosystem" (Tier 4) → "personality / cosmetic" (Tier 5) → "cross-cutting plumbing" (Tier 6) → "already won't-do" (Tier 7) → "deliberate-skip / different-positioning" (Tier 8). Anything in Tiers 1-3 is what to look at first if you only have a week.

---

## Executive summary

Most "obvious" Hermes features are already shipped or actively spec'd in OpenComputer — the 28-item Tier S port (PR #171), the 9-channel Track B already shipped, the channel-feature port spec (15.5 days, 6 PRs queued), pluggable Layer 3, voice-mode, browser-control, auto-skill-evolution, ambient-sensor, and the Sub-projects A/B/C/D/E ship gates have collectively closed ~80% of the public Hermes surface. **The remaining gaps cluster into three real strategic stories:**

1. **Skills as a network ecosystem.** Hermes skills are not just local SKILL.md files — they're connected to a multi-registry hub (`agentskills.io`, `clawhub`, `skills-sh`, `lobehub`, `well-known`, `github`) with `browse / search / install / inspect / publish / tap / snapshot / audit` and a `Skills Guard` LLM-scanner gate. OpenComputer ships a quarantine→approve evolution loop and a guard policy module, but the *network discovery + publish + tap* surface is entirely missing. **This is the single largest visible-to-user gap.**

2. **First-class generative tools.** Hermes treats `image_generate`, `vision_analyze`, `text_to_speech`, `mixture_of_agents`, and `send_message` as **bundled tools the agent calls by name** — not as plugin-scoped utilities. OC has the underlying capability for most of these (FAL via dev-tools, voice tools, MCP `messages_send`, multimodal content blocks for vision) but the *tool registration shape* is fragmented. Promoting these to first-class core tools changes how often the model uses them.

3. **Power-user CLI/TUI ergonomics.** A long tail of small, individually-tractable wins: `/branch`, `/btw`, `/skin`, `/copy`, `/snapshot`, `/rollback`, `/queue`, `/reasoning`, `/fast`, `@filepath` autocomplete, external-editor for prompts, `--worktree -w` flag, `hermes backup / import`, `hermes profile alias / clone / export / import`, `hermes hooks list / test / revoke`, model aliases in config, OpenRouter provider routing knobs, response-pacing (`human_delay`), session-reset auto-policy, auto-prune at startup, more TTS providers (Edge TTS is **free** — no API key), more STT providers (Groq is dirt-cheap), curses session browser parity. Individually small; collectively this is the difference between "a working agent" and "a delightful agent."

**OC's distinguishing stack** — Layered Awareness L0-L4, Life-Event Detector, plural personas with vibe classifier, ambient foreground sensor, auto-skill-evolution, F1 consent layer with HMAC audit chain, F4 user-model graph, F5 decay/drift, instruction detector, OI removal → native introspection, shipped Sub-projects A/B/C/D/E — has **no Hermes equivalent**. The gap analysis below is *one direction*: it does not enumerate the dozens of OC capabilities Hermes lacks. (For that direction, see `docs/audit/05-skills-tools-plugins.md` and the awareness section in the parity audit.)

**If you only port five things from this doc, port these:**

1. **Skills Hub network layer** (browse/search/install/publish/tap + agentskills.io standard) — Tier 1.A
2. **First-class `ImageGenerateTool` + `VisionAnalyzeTool` + `SendMessageTool` + `MixtureOfAgentsTool`** — Tier 1.B
3. **Slash-command bundle** (`/branch /btw /snapshot /skin /copy /image /paste /reasoning /fast /queue`) — Tier 2.A
4. **`hermes backup / import` + profile clone / alias / export / import** — Tier 3.E
5. **Edge TTS (free) + Groq STT** — Tier 4.B (because zero-cost wins are zero-cost wins)

Everything else is correctly classified Tier 3+ or already on a runway.

---

## Methodology

This audit is grounded in three parallel deep extractions run 2026-04-28:

- **Hermes inventory pass:** read `sources/hermes-agent-2026.4.23/` exhaustively — `cli.py`, `hermes_cli/`, `agent/` (43 modules), `gateway/platforms/` (27 channels), `cron/`, `acp_adapter/`, `acp_registry/`, `plugins/` (7 bundled), `tools/` (40+ tools), `environments/` (RL — out-of-scope), `scripts/install.sh`, `cli-config.yaml.example` (1000+ lines of config schema), `SECURITY.md`, all `RELEASE_v*.md` notes through v0.11.0, plus public docs at `hermes-agent.nousresearch.com/docs/`.
- **OC parity pass:** walked the working repo at `/Users/saksham/Vscode/claude/OpenComputer/` — `opencomputer/` core, `extensions/` (27 plugins now, not 7 — that count in CLAUDE.md is stale), `plugin_sdk/`, `docs/`, plus PR history through #219 to verify what each prior memory entry actually shipped.
- **Prior-art pass:** read `docs/refs/hermes-agent/inventory.md`, `docs/superpowers/specs/2026-04-28-hermes-channel-feature-port-design.md`, `docs/audit/05-skills-tools-plugins.md`, `docs/audit/06-privacy.md`, `docs/superpowers/specs/2026-04-27-platform-reach-port-guide.md`, plus the 11 other April 2026 specs to ensure this doc does not re-propose work that's already been spec'd, decided, or punted.

**Status notation used throughout:**

- **`[shipped]`** — verified present in current code with PR/file pointer.
- **`[partial]`** — capability exists but in a different shape, scoped narrower, or missing key sub-features.
- **`[missing]`** — no equivalent in code; truly a gap.
- **`[deliberate-skip]`** — present in Hermes, deliberately not ported, with cited reason.
- **`[wont-do]`** — explicitly in CLAUDE.md §5 Tier 5 ("Won't do — Reopen only if a concrete use case appears").
- **`[planned]`** — already in an approved spec or in-flight PR; not a real gap.
- **`[N/A]`** — not applicable to OC's positioning (e.g., Nous Portal OAuth).

**Effort sizing:**

- **S** = ≤1 day (single-file change, narrow blast radius, minimal tests)
- **M** = 1-3 days (cross-module, multiple files, moderate tests, possibly new SDK type)
- **L** = 3-7 days (new subsystem or plugin, full test suite, cross-cutting integration)
- **XL** = 1-3 weeks (architectural shift, multiple PRs, breaking-or-near-breaking changes, ecosystem implications)

---

## Quick scoreboard (per-category coverage)

| Category | Hermes surface size | OC shipped | OC partial | OC missing | OC won't-do / N/A |
|---|---|---|---|---|---|
| Subcommands (`hermes`/`oc`) | 36 | 33 | 1 | 1 | 1 |
| Slash commands (in agent loop) | 51 | 17 | 4 | 26 | 4 |
| Channels / messaging | 18 active | 13 | 5 | 0 | 7 (Asia + BlueBubbles) |
| Memory backends | 7 plugins | 1 (Honcho) | 0 | 6 | (deliberate — port ABC only) |
| Skills | local + 6 hubs + tap + audit | local + guard | quarantine→approve | hub network | (none) |
| Tools (core) | 40+ | ~45 (we counted higher!) | 5 | 4 | 0 |
| Terminal backends | 6 | 5 (local/docker/ssh/macOS-sandbox/linux-bwrap) | 0 | 0 | 3 (daytona/singularity/modal) |
| TUI / interactive CLI | ~25 features | 17 | 2 | 6 | 0 |
| Cron / scheduling | full | full | minor (per-job toolset) | 0 | 0 |
| MCP integration | full | full | minor (gemini provider) | 0 | 0 |
| Security primitives | ~12 | 10 | 2 | 0 | 0 |
| Configuration | huge schema | most | model_aliases / OpenRouter routing | a few | 0 |
| Personalities / personas | 14 built-in | 6 (different model) | persona vs personality nomenclature mismatch | hermes-style flat name list | 0 |
| Voice / multimodal | 8 TTS + 5 STT + vision + image-gen | 1 TTS + 2 STT + paste-vision + FAL | 4 TTS + 3 STT | wake-word | wake-word |
| Plugin / extension architecture | 4-source discovery + 13 hooks + commands + dispatch | similar shape | acp_registry multi-IDE | (none big) | 0 |
| Installer / packaging | ubuntu/macos/wsl2/termux/nixos | ubuntu/macos/termux | wsl2/nixos | windows-native | windows-native |
| Web dashboard | full React + i18n + plugins | dashboard exists | i18n + theme + plugin tabs | (we ship a slimmer one) | 0 |
| Awareness / passive education | (none) | full L0-L4 + LED + companion + ambient + LM | — | — | (Hermes has no equivalent) |
| Research / RL | full Atropos + benchmarks + parsers | trajectory export only | — | — | full Atropos integration |

**Net read:** OC is at parity or beyond on memory/security/awareness/cron/MCP/architecture; behind on the *network ecosystem of skills*, the *first-class shape of generative tools*, and a long tail of *power-user TUI/CLI ergonomics*.

---

## Already planned / in-flight (do NOT re-propose)

These are captured in approved specs and are scheduled. Listed here so a future synthesizer doesn't double-count them as "gaps."

- **Channel feature port (Tier 1+2+3 of `2026-04-28-hermes-channel-feature-port-design.md`)** — shared `plugin_sdk/channel_helpers.py`, `channel_utils.py`, `network_utils.py`, `format_converters/`; BaseChannelAdapter retry + reaction lifecycle; mention-boundary safety; phone redaction; email automated-sender filter; photo-burst merging in Dispatch; sticker vision cache; webhook deliver_only mode; Telegram polling fatal cap; bare local file auto-detection; `MEDIA:` directive parsing; Telegram IP-fallback transport; Discord allowed_mentions; webhook idempotency cache; Slack pause-typing; Slack mrkdwn / Matrix HTML converters; webhook cross_platform mode; DM Topics; Matrix E2EE; WhatsApp Baileys bridge; Discord forum threads + slash command tree. **Owner:** ~15.5 dev-days budget, 6 PRs planned.
- **Pluggable Layer 3 extractor (Ollama/Anthropic/OpenAI)** — PR #208, with smart-fallback prompt PR #209. Done.
- **Voice mode (continuous push-to-talk + VAD + local Whisper fallback)** — PR #199.
- **Browser control (5 Playwright tools)** — PR #202.
- **Auto-skill-evolution loop** — PR #193 + adapter fix #204.
- **Ambient foreground sensor** — PR #184.
- **Layered Awareness V2.C (Life-Event Detector + plural personas)** — PR #163.
- **Companion voice + cross-persona vibe + affect injection plugin** — PR #213+#218+#219.
- **TUI Phase 1 (PromptSession + ESC/Ctrl+C cancel + 8 slash commands)** — PR #180.
- **TUI Phase 2 (slash autocomplete with visible dropdown)** — PR #200/#214.
- **Resume picker (21 hard E2E tests)** — PR #212.
- **OI removal → native cross-platform introspection** — PR #179.
- **Tier S 28-item Hermes port** — PR #171: prompt caching, tool-result spillover, OSV malware, URL/SSRF guard, subdirectory hints, async title generator, cross-session rate-limit guard, plus 21 smaller items.

If a feature appears in this list, *do not include it in the "missing" tally below.* Cross-check anything in the Tier 1-6 lists against the planned list before scoping.

---

## Tier 1 — High-leverage strategic gaps (recommended for the next ship-wave)

These are the gaps that visibly change OC's positioning. Each is **demonstrably absent**, **strategically important**, and **not blocked on prior work**.

### Tier 1.A — Skills Hub network layer (`agentskills.io` + multi-registry browse/install/publish)

**What it is:** Hermes skills aren't just local SKILL.md files. The agent and the human can both `browse`, `search`, `install`, `inspect`, `update`, `audit`, `uninstall`, `reset`, `publish`, `snapshot`, `tap`, and `config` skills against six remote registries — `official`, `skills-sh`, `well-known`, `github`, `clawhub`, `lobehub`. Every install/removal is logged to `~/.hermes/skills/.hub/audit.log`. Third-party `tap` lets any user add an arbitrary GitHub repo as a skill source. The `Skills Guard` LLM-scanner gates third-party installs with a security review. There's a public `agentskills.io` interop standard that defines the SKILL.md frontmatter so skills work across compatible agents.

**Hermes pointers:**
- CLI: `hermes skills [browse|search|install|inspect|list|check|update|audit|uninstall|reset|publish|snapshot|tap|config]` — `hermes_cli/skills_hub.py`, `hermes_cli/main.py:L7711-L7848`
- Tool: `skill_hub` (agent-facing) — `tools/skills_hub.py`
- Slash: `/skills [search|browse|inspect|install]` — `hermes_cli/commands.py:L135`
- Standard: README mentions `agentskills.io` as the open interop standard
- Guard: `tools/skills_guard.py` + `config.skills.guard_agent_created`
- Audit: `~/.hermes/skills/.hub/audit.log`

**OC current state:** **`[partial]`**
- Local skills: shipped — 40 bundled in `opencomputer/skills/`, plus per-profile `~/.opencomputer/<profile>/skills/`.
- `SkillTool` (read) + `SkillManageTool` (write) — shipped.
- `Skills Guard` policy module + threat scanner — shipped (PRs #76, #165). Trust-tier policy is in place.
- Auto-skill-evolution (quarantine → approve → archive) — shipped (PR #193).
- `opencomputer skills_guard/policy.py` strips `skills-sh/...` prefixes as **aliases only** — no live registry sync, no remote install, no publish.
- **Missing:** browse/search/install against any remote registry, publish flow, tap (add-arbitrary-GitHub), snapshot export/import for skill collections, public network audit log, `agentskills.io` standard validator.

**Why it matters:** This is OC's most visible network gap. Right now an OC user can only run skills they've manually authored or that ship bundled. They can't `oc skills install pead-screener` and have it appear from a hub. They can't publish an in-house skill to share with teammates without git-hosted custom infrastructure. The `agentskills.io` standard means a Hermes user's skill collection works on Hermes — an OC user can't trivially import that. The auto-skill-evolution loop synthesizes new skills locally but has no destination to publish them to.

**Effort:** **L → XL**. Breakdown:
- **L (5-7 days)** for a minimal viable hub: `oc skill browse|search|install|inspect|list|update|uninstall` against one default registry (probably `agentskills.io` since it's the open standard), Skills Guard runs on install, audit log appended on every install/removal, `~/.opencomputer/<profile>/skills/.hub/` becomes the hub-managed directory.
- **+M (2-3 days)** for `tap add|remove|list` (arbitrary GitHub repo as source).
- **+M (2-3 days)** for `publish --to <hub>` flow with manifest validation.
- **+S (1 day)** for `snapshot export|import` (`.tar.gz` of installed-hub-skills directory).
- **+M (2-3 days)** for `agentskills.io` frontmatter validator + interop tests.

**Prereqs / blockers:** None hard. Soft prereq: decide which hub to default to. Recommendation: `agentskills.io` (open standard, vendor-neutral) with `clawhub` and `well-known` as opt-in additional taps.

**Risk if skipped:** OC stays stuck at "every skill is hand-authored or quarantine-evolved" forever. No network effects. Auto-skill-evolution has no audience to publish to. Users on Hermes can't bring their skill libraries over. Long-term this is a wedge that compounds against OC.

**Recommendation:** **Port — start with the L slice (browse/search/install + Skills Guard gate + audit log) targeting `agentskills.io` as the default registry. Add tap/publish/snapshot in a follow-up PR.**

---

### Tier 1.B — First-class generative tool registrations (`ImageGenerateTool`, `VisionAnalyzeTool`, `SendMessageTool`, `MixtureOfAgentsTool`)

**What it is:** Hermes registers four "headline" generative/coordination tools in the core registry under stable names so the model reaches for them by reflex — `image_generate`, `vision_analyze`, `send_message`, `mixture_of_agents`. They're toolset-presets-aware (e.g., the `safe` preset includes `vision` + `moa` but not `terminal`).

**Hermes pointers:**
- `tools/image_generation_tool.py` — FAL backends (FLUX Pro 1.1, Recraft V4, GPT Image 2, xAI grok-imagine, configurable via `IMAGE_GEN_DEFAULT_MODEL`)
- `tools/vision_tools.py` — `vision_analyze` calls multimodal LLM via auxiliary client; supports URL or base64 input; SSRF guard + redirect re-validation
- `tools/send_message_tool.py` — sends to any configured platform channel (`telegram`, `slack`, `discord`, `email`, etc.) by name
- `tools/mixture_of_agents_tool.py` — voting across multiple OpenRouter models for high-stakes reasoning; requires `OPENROUTER_API_KEY`

**OC current state:**
- **Image generation:** `[partial]`. `extensions/dev-tools/fal_tool.py` exists with `FalTool` and reaches FAL inference, but it's scoped to the dev-tools extension (which is `kind=tools` and not in the default toolset), so the model often doesn't see it. No core-tool registration. No multi-backend fallback. Inventory verdict was `port` — never executed.
- **Vision:** `[partial]`. Image paste is supported (PR #182+#183), Anthropic + OpenAI multimodal content blocks are wired into the loop, but **no standalone `VisionAnalyzeTool` exists**. The agent can describe images that are *already in the conversation* but cannot say "here's a URL, analyze the image at it" without reaching for `web_fetch` and getting bytes — which won't work for non-image URLs.
- **Send message:** `[partial]`. MCP server exposes `messages_send(platform, chat_id, body)` (PR #175), so the agent can reach it via the MCP-as-meta-tool path. **No first-class `SendMessageTool` in the core registry.** Cron's `_deliver()` handles cron-triggered delivery but not arbitrary agent-driven sends.
- **Mixture of Agents:** `[missing]`. `Delegate` covers single-spawn but there's no voting primitive. Inventory verdict was `skip` ("Delegate covers single-spawn") — but Delegate doesn't compose like MoA does. The skip verdict undervalues the consensus-voting use case.

**Why it matters:**
- *Image generation:* Most agent workflows that "could use an image" don't ask because the tool isn't visible. Promoting it surfaces it in the schema list every turn.
- *Vision analyze:* "Here's a URL of a chart, summarize it" should Just Work. Right now it doesn't.
- *Send message:* Cross-platform proactive sends (cron, on-demand "remind me on telegram", "ping the team in slack") want a first-class tool, not an MCP indirection. The MCP wrapper is more brittle (server has to be running) and slower.
- *MoA:* High-confidence reasoning tasks (security review, financial advice, irreversible decisions) benefit from voting. Today: not available.

**Effort:**
- **`ImageGenerateTool`:** S-M (1-2 days) — promote `FalTool` shape into `opencomputer/tools/image_gen.py`, register in core, add multi-backend fallback (FAL + OpenAI Images + xAI grok-imagine), add `image_gen.default_model` config.
- **`VisionAnalyzeTool`:** S-M (1-2 days) — wire to auxiliary client, accept URL or base64, run `is_safe_url` + magic-byte sniff before fetching, return text description. Reuse OpenAI/Anthropic multimodal content-block support.
- **`SendMessageTool`:** S (1 day) — wrap `gateway.dispatch._deliver()` so the agent can call it directly; respect channel allowlists; emit consent claim if non-main session.
- **`MixtureOfAgentsTool`:** M (2-3 days) — config-driven model list, parallel calls via `asyncio.gather`, simple voting (majority text response or specifically-rated answer), token-cost surfaced via `model_metadata`.

**Prereqs / blockers:**
- `model_metadata` for cost-attribution on MoA (already shipped per PR #120, so no actual blocker).
- Consent layer claims for `SendMessageTool` (already shipped, just needs claim string).

**Risk if skipped:** Continued gap between "OC has the capability" and "the agent reaches for it." A user asking "make me a chart" gets a written response describing the chart instead of an image; a user asking "what's in this URL screenshot" gets "I can't see images at URLs."

**Recommendation:** **Port all four. Order: SendMessageTool (S, low risk) → VisionAnalyzeTool (S-M) → ImageGenerateTool (M) → MixtureOfAgentsTool (M, can defer if 2026-Q3 priorities push it).**

---

### Tier 1.C — Web dashboard polish (i18n, theme system, plugin tabs, real-time API tracking)

**What it is:** Hermes ships a full React/Vite dashboard at port 9119 (`hermes dashboard`) with English+Chinese i18n, a live-switching theme system, react-router sidebar layout (sticky header, mobile-responsive), per-session API call tracking, one-click update + gateway restart buttons, HTTP health probe for cross-container detection, deployable to Vercel, and — critically — a **dashboard plugin system** where third-party plugins add tabs/widgets without forking. v0.11.0 release notes call this out as a major investment area.

**Hermes pointers:**
- `web/` (React/Vite/TypeScript)
- `plugins/example-dashboard/` (template for adding a tab)
- `plugins/strike-freedom-cockpit/theme/` (theme provider example)
- CLI: `hermes dashboard` — `hermes_cli/main.py:L8733`

**OC current state:** **`[partial]`**
- A dashboard exists at `opencomputer/dashboard/` per the audit, plus `extensions/browser-bridge/` (Phase 8.A) for the Chrome extension side of Layered Awareness Layer 4.
- **What we have:** session viewer, basic agent control, layered-awareness layer indicators (last verified state).
- **What we're missing:** i18n, theme system, plugin tabs (third-party-extensible widgets), real-time API call tracker (per-session token + cost), one-click update, gateway restart UI, mobile-responsive layout, Vercel-deploy template.

**Why it matters:** A dashboard is the easiest demo surface — it's what people see in screenshots. Hermes's investment in this area gives them a polish moat. OC's dashboard is functional but feels skeletal next to the screenshots in v0.11.0 release notes. **For the GitHub README and any future "OpenComputer 2026" thread, dashboard polish punches above its dev-cost weight.**

**Effort:** **L (5-7 days)** if we accept Hermes's design choices wholesale. Breakdown:
- **M (2-3 days)** — port theme provider with 4 starter themes (default, ares, mono, slate per Hermes) using CSS variables.
- **S (1 day)** — i18n setup (English-only at first, Chinese as a follow-up if demand surfaces — this is the *infrastructure*, not the translations).
- **M (2-3 days)** — plugin-tab API: `dashboard_tab` registration in `PluginAPI`, dashboard reads bundled tabs at boot, hot-reload on plugin enable/disable.
- **S (1 day)** — real-time API call tracker (subscribe to event bus → push tokens-used per session over WebSocket).
- **S (1 day)** — gateway restart button + one-click update prompt.

**Prereqs / blockers:** None — `opencomputer/dashboard/` already exists as scaffolding.

**Risk if skipped:** Dashboard remains a "we have one" capability rather than "look at this thing." Demo-and-screenshot disadvantage on social.

**Recommendation:** **Defer until post-v1.0.** This is "ship-shape polish" not "core capability." Dogfood-gate it. *But* — if any external visibility moment comes up (release announcement, Hacker News post, Twitter thread), pull this forward before the moment.

---

### Tier 1.D — `agentskills.io` standard compatibility (interop with Hermes/Claude-skills ecosystem)

**What it is:** Hermes documents skills as compatible with the `agentskills.io` open standard. The standard defines SKILL.md frontmatter (`name`, `description`, `version`, `author`, `tags`, `requires`, optional `tools`) so any agent that ships an `agentskills.io`-compatible runtime can consume any compatible skill.

**Hermes pointers:** README highlights `agentskills.io` as the standard. Internally, Hermes's `tools/skills_hub.py` validates skills against the standard before install.

**OC current state:** **`[partial]`**
- OC's SKILL.md uses a frontmatter (`name`, `description`, etc.) that's *similar to but not identical* to the `agentskills.io` shape.
- No validator. No declared compatibility statement. No interop test that "a known-good `agentskills.io` skill loads in OC unchanged."

**Why it matters:** Standards-based interop is a 10x better story than vendor-locked skills. OC + Hermes + Claude-skills + the rest of the ecosystem all converging on the same SKILL.md shape means any user can move libraries freely. *This is half of Tier 1.A* — without standard compat, "skills hub" still means "yet another vendor-specific format."

**Effort:** **S-M (1-2 days)** — write an `agentskills.io` frontmatter validator, run it against all bundled skills, add a CI test that loads a known-good external skill, document compat in README.

**Prereqs / blockers:** Need to read the actual `agentskills.io` spec (we currently just know it exists from Hermes's README and an earlier inventory note). If the spec is still draft/unstable, we can shadow it informally and tighten later.

**Risk if skipped:** OC's skills are "almost compatible" forever, which is worse than declared incompatibility because it creates silent footguns when users assume interop.

**Recommendation:** **Port together with Tier 1.A** — they're the same bet.

---

### Tier 1.E — `MixtureOfAgentsTool` strategic re-evaluation

This is broken out separately because the original 2026-04-22 inventory verdict was `skip` ("Delegate covers single-spawn"), but the audit re-reading suggests that verdict undervalues the use case. See Tier 1.B for the implementation; here's the *reason* to revisit:

- **Delegate ≠ MoA.** Delegate spawns *one* worker with isolated context to do *one* task. MoA runs *N* models in parallel on the *same* prompt and votes. Different thing entirely.
- **Trust-tier amplification.** For high-stakes reasoning (medical, financial, irreversible operations) MoA is the recognized way to push beyond single-model reliability. We invest heavily in trust-tier consent — pairing it with single-model output undersells the consent investment.
- **Cheap-route gate compatibility.** Cheap-route already routes simple turns to a single small model. MoA naturally complements: route easy turns to one model, route flagged-hard turns to MoA. The architecture supports this without changes.

**Recommendation:** **Re-classify from `skip` to `port` (M effort).** Add to Tier 1.B port wave.

---

## Tier 2 — Power-user UX gaps (high reward / low risk / individually small)

These are *individually small* features that **collectively** make the difference between "a working agent" and "a delightful agent." Most are S-effort. Most have zero dependencies. Most can be parallelized.

### Tier 2.A — Slash-command bundle (10 missing slash commands)

OC currently ships `/help`, `/clear`, `/reset`, `/plan`, `/code`, `/persona`, `/steer`, `/models`, `/rename`, `/resume`, `/skill`, plus the agent slash dispatcher. That's 12 commands. Hermes ships **51**. The gap list, by impact:

#### Tier 2.A.1 — `/branch [name]` (alias `/fork`)

**What:** branch the conversation to explore a different path. Forks the session at the current turn, tags it with `name`, lets the user switch back later.

**Hermes:** `hermes_cli/commands.py:L71`

**OC state:** `[missing]`. We have `oc session fork` CLI (PR #121) but no in-loop slash command. Audit: "[shipped]; PR #121" for the CLI; no slash equivalent.

**Effort:** **S (4-6 hrs)** — wire CLI fork to slash dispatcher, add to slash autocomplete.

**Why it matters:** Exploratory thinking. "Try this approach... no, branch back, try that approach." Without `/branch`, users either rewrite the prompt manually (losing context) or tolerate a noisy linear history.

**Recommendation:** Port. S effort, high UX value.

---

#### Tier 2.A.2 — `/btw <question>` (ephemeral side question)

**What:** ask a question that uses the current session context but **doesn't run any tools**, **isn't persisted**, and **doesn't compress the parent context window**. Pure "by the way, quickly..."

**Hermes:** `hermes_cli/commands.py:L87`

**OC state:** `[missing]`. We have `/queue` and `/steer` (latter shipped, PR #125), but no ephemeral side-channel.

**Effort:** **S (4-6 hrs)** — fork a sub-loop with isolated history append-only, hide tools, return text only.

**Why it matters:** "I'm in the middle of debugging — by the way, what was that command for X again?" — without breaking the debug session's context.

**Recommendation:** Port. The simplest UX win in this whole list.

---

#### Tier 2.A.3 — `/snapshot [create|restore <id>|prune]` (config/state snapshots)

**What:** snapshot of OC config + selected state files (memory, skills, config.yaml). Restore lets you roll back. Different from filesystem rollback (which is per-tool checkpoint).

**Hermes:** `hermes_cli/commands.py:L79`

**OC state:** `[missing]`. We have F1 audit chain but it's append-only, not snapshot-restore.

**Effort:** **M (2 days)** — design what gets snapshotted (config.yaml + MEMORY.md + USER.md + active skills directory listing + active personas + plugin enable/disable list); store as `.tar.gz` in `~/.opencomputer/<profile>/snapshots/<id>/`; restore reverses.

**Why it matters:** Safe experimentation. "Try a config change, snapshot first, try, roll back if bad." Especially useful when fiddling with awareness/persona settings.

**Recommendation:** Port. M effort, broad UX value.

---

#### Tier 2.A.4 — `/rollback [number]` (filesystem checkpoints)

**What:** list or restore filesystem checkpoints created by Edit/Write/MultiEdit operations. Different from `/snapshot` (which is config-state).

**Hermes:** `hermes_cli/commands.py:L77`. Hermes ships `tools/checkpoint_manager.py`.

**OC state:** `[partial]`. We have `CheckpointDiff` (PR #29) and a `RewindTool` per the audit, but the slash-command surface is incomplete.

**Effort:** **S (1 day)** — wire existing checkpoint primitives to slash dispatcher with list/restore subcommands.

**Why it matters:** Coding-mode safety net. "I let it edit 5 files, hated the result, /rollback 3."

**Recommendation:** Port. S effort, makes coding-harness less scary.

---

#### Tier 2.A.5 — `/skin [name]` (theme engine)

**What:** Hermes has 4 built-in skins (`default`, `ares`, `mono`, `slate`) plus `~/.hermes/skins/<name>.yaml` for custom. Each skin sets colors, spinner faces, "thinking" verbs, "wings" (border characters). `/skin` switches.

**Hermes:** `hermes_cli/skin_engine.py`

**OC state:** `[missing]`. We have rich.Console used directly with default colors. No theming, no skins.

**Effort:** **M (1.5 days)** — port skin_engine.py shape, ship 3 starter skins (light/dark/mono), let users add YAMLs.

**Why it matters:** Personality. Some users want bright; some want monochrome; some want pirate-themed spinners. Cheap polish that disproportionately influences "feels like home" perception.

**Recommendation:** Port. M effort, low risk, high charm.

---

#### Tier 2.A.6 — `/copy [number]` (OSC-52 clipboard)

**What:** copy the last assistant response to the clipboard via OSC-52 escape sequence (works over SSH/tmux without xclip).

**Hermes:** `hermes_cli/commands.py:L161`

**OC state:** `[missing]`. Image *paste* is shipped (PR #182), but copy isn't.

**Effort:** **S (2-3 hrs)** — emit OSC-52 sequence with the last assistant turn's text.

**Why it matters:** Friction reduction. Users currently mouse-select-and-copy.

**Recommendation:** Port. Trivial effort, daily-use win.

---

#### Tier 2.A.7 — `/image <path>` (attach local image as input)

**What:** attach an image file to the next prompt without going through clipboard.

**Hermes:** `hermes_cli/commands.py:L165`

**OC state:** `[partial]`. Clipboard paste works (PR #182). File-path attach is a different code path — verify; if missing, add.

**Effort:** **S (3-4 hrs)** — read file → base64 → wrap as multimodal content block in next user turn.

**Why it matters:** Vision workflows that don't fit clipboard (large screenshots, batch images).

**Recommendation:** Port if missing.

---

#### Tier 2.A.8 — `/reasoning [level|show|hide]` (control thinking display + effort)

**What:** Hermes lets the user set reasoning effort (`xhigh|high|medium|low|minimal|none`) and toggle whether `<think>` blocks render. `/reasoning show` reveals, `/reasoning hide` strips.

**Hermes:** `hermes_cli/commands.py:L119`

**OC state:** `[partial]`. We have extended thinking via Anthropic provider but no slash control. Reasoning blocks are filtered automatically.

**Effort:** **S-M (1 day)** — slash command sets `agent.reasoning_effort` for the session; show/hide toggle binds to the streaming filter.

**Why it matters:** When extended thinking is enabled, sometimes users want to *see* it (debugging an answer), sometimes want it hidden (presentation context). Currently no toggle.

**Recommendation:** Port. M effort, valuable for power users.

---

#### Tier 2.A.9 — `/fast [normal|fast|status|on|off]` (priority service tier)

**What:** toggle OpenAI Priority / Anthropic Fast Mode. Priority means higher cost but lower latency.

**Hermes:** `hermes_cli/commands.py:L122`

**OC state:** `[missing]`. We have CostGuard but no priority-tier toggle.

**Effort:** **S (4-6 hrs)** — set `model.service_tier: priority` for the session; provider plugins respect it.

**Why it matters:** Live demos, time-sensitive tasks, "I'll pay more for speed right now."

**Recommendation:** Port. Trivial.

---

#### Tier 2.A.10 — `/queue <prompt>` (alias `/q`) (queue prompt for next turn)

**What:** queue a follow-up prompt that fires automatically when the current turn finishes. Different from `/steer` (mid-turn nudge); `/queue` is *next-turn pre-fill*.

**Hermes:** `hermes_cli/commands.py:L92`

**OC state:** `[missing]`. `/steer` is shipped (PR #125), `/queue` is not.

**Effort:** **S (1 day)** — store queued prompt in session state; consume when current turn enters idle.

**Why it matters:** Multi-step workflow planning. "Once this finishes, do X." Without queue, user has to wait + re-prompt.

**Recommendation:** Port. S effort, high productivity for power users.

---

#### Tier 2.A.11 — `/title [name]` (set session title)

**What:** rename the current session. Replaces auto-generated title.

**Hermes:** `hermes_cli/commands.py:L70`

**OC state:** `[partial]`. We have `/rename` (PR #186). Verify it does the same thing.

**Effort:** **S (verify)** — likely already shipped under different name.

---

#### Tier 2.A.12 — `/save` (save current conversation to disk as standalone file)

**What:** export current conversation as a markdown/JSON file outside the session DB.

**Hermes:** `hermes_cli/commands.py:L66`

**OC state:** `[missing]`. We have `oc session export` CLI per audit.

**Effort:** **S (4 hrs)** — wire existing export to slash.

**Recommendation:** Port slash wrapper.

---

#### Tier 2.A.13 — `/history` (in-loop history viewer)

**What:** show the conversation history in scrollable form within the TUI without leaving the session.

**Hermes:** `hermes_cli/commands.py:L64`

**OC state:** `[missing]` as in-loop slash. CLI: `oc sessions show <id>` exists.

**Effort:** **S (4-6 hrs)** — render current session history with rich.

**Recommendation:** Port. Useful when context window is huge.

---

#### Tier 2.A.14 — `/agents` (alias `/tasks`) (show active agents and running tasks)

**What:** list all currently-running detached tasks and delegated subagents with status.

**Hermes:** `hermes_cli/commands.py:L89`

**OC state:** `[partial]`. Detached tasks shipped (PR #173), `oc task list/status` CLI exists. Slash wrapper missing.

**Effort:** **S (2 hrs)** — slash dispatch.

**Recommendation:** Port slash wrapper.

---

#### Tier 2.A.15 — `/usage` (show token + rate-limit status in-loop)

**What:** in-loop view of session token usage and provider rate limit status.

**Hermes:** `hermes_cli/commands.py:L156`

**OC state:** `[partial]`. CostGuard CLI shipped; insights CLI shipped (PR #168). In-loop slash missing.

**Effort:** **S (4 hrs)** — slash that pulls from CostGuard + rate_guard.

---

#### Tier 2.A.16 — `/insights [days]` (analytics in-loop)

**What:** in-loop view of usage analytics, top tools, top sessions.

**Hermes:** `hermes_cli/commands.py:L157`

**OC state:** `[partial]`. CLI `oc insights` shipped (PR #168). Slash wrapper missing.

**Effort:** **S (4 hrs)**.

---

#### Tier 2.A.17 — `/platforms` (alias `/gateway`) (gateway/messaging platform status)

**What:** show active platform connections, last poll time, message queue depth.

**Hermes:** `hermes_cli/commands.py:L159`

**OC state:** `[partial]`. `oc channels status` CLI shipped. Slash wrapper missing.

**Effort:** **S (3 hrs)**.

---

#### Tier 2.A.18 — `/voice [on|off|tts|status]` (voice mode toggle)

**What:** toggle voice mode within session.

**Hermes:** `hermes_cli/commands.py:L127`

**OC state:** `[partial]`. Voice mode shipped (PR #199), `oc voice` CLI exists. Slash wrapper missing.

**Effort:** **S (2 hrs)**.

---

#### Tier 2.A.19 — `/yolo` (toggle YOLO mode = skip approvals)

**What:** in-loop toggle to skip the consent gate for the rest of the session.

**Hermes:** `hermes_cli/commands.py:L117`

**OC state:** `[partial]`. F1 consent gate has `--bypass` flag; no in-loop toggle.

**Effort:** **S (3 hrs)** — slash sets a session-scoped consent bypass; F1 audit log records the elevation.

**Risk:** YOLO is dangerous. Implement with explicit "are you sure? yolo until /unyolo or session end?" prompt and full audit trail.

---

#### Tier 2.A.20 — `/personality [name]` (predefined personality from config)

**What:** Hermes has 14 built-in personalities (`helpful, concise, technical, creative, teacher, kawaii, catgirl, pirate, shakespeare, surfer, noir, uwu, philosopher, hype`). `/personality` switches.

**Hermes:** `hermes_cli/commands.py:L110`. `cli-config.yaml.example:L527-541`.

**OC state:** `[partial]`. We have `/persona` (PR g45) with 6 plural personas (admin/coding/companion/learning/relaxed/trading). **Note: persona ≠ personality in Hermes' nomenclature** — Hermes' personas are *role-based* prompt wrappers; OC's personas are *context-based* (which persona is active depends on detected user state). Different concepts.

**Effort:** **S (1 day)** — add a flat `personalities` section to config.yaml that maps short names → prompt overlays; new `/personality` slash separate from `/persona`. Optionally bundle a "fun" set (pirate / shakespeare / etc.) if the user wants flavor.

**Recommendation:** Port. Different from our personas — both are useful.

---

#### Tier 2.A.21 — `/skin`, `/statusbar`, `/verbose` (display toggles)

`/skin` — see Tier 2.A.5.

`/statusbar` (alias `/sb`) — toggle context/model status bar in the TUI. `[missing]` likely. **S (2 hrs).**

`/verbose` — cycle tool progress display modes (off/new/all/verbose). `[partial]` — we have streaming on/off but not the 4-mode cycle. **S (3 hrs).**

---

#### Tier 2.A.22 — `/reload`, `/reload-mcp` (hot-reload)

`/reload` — reload `.env` variables into running session without restart.

`/reload-mcp` — reload MCP servers from config without restart.

**Hermes:** `hermes_cli/commands.py:L141, L143`

**OC state:** `[missing]`. We have MCP `connect/disconnect` per audit but no full reload primitive.

**Effort:** **S-M (1 day)** for both — careful to invalidate cached provider clients on `.env` reload.

**Recommendation:** Port. Big DX win for users editing env without restarting.

---

#### Tier 2.A.23 — `/browser [connect|disconnect|status]` (CDP attach to live Chrome)

**What:** attach the agent's browser tools to an *already-running* Chrome instance via CDP, instead of spawning Playwright Chromium each time. User keeps their tabs/sessions/cookies; agent operates on their browser.

**Hermes:** `hermes_cli/commands.py:L144`. Auto-detects Chrome/Chromium/Brave/Edge.

**OC state:** `[missing]`. browser-control extension (PR #202) uses isolated Playwright session per call.

**Effort:** **M (2 days)** — port CDP attach, start each browser tool with `connect_over_cdp` instead of `launch`.

**Why it matters:** Logged-in workflows. Currently the agent can't operate on sites where you're authenticated unless you re-auth in its sandbox.

**Recommendation:** Port. M effort, big enabler for personal workflow automation.

---

#### Tier 2.A.24 — `/restart` (graceful gateway restart with drain)

**What:** drain active runs, then restart the gateway daemon.

**Hermes:** `hermes_cli/commands.py:L154`. Gateway-only.

**OC state:** `[missing]`. We have `oc gateway start/stop` but no drain-restart.

**Effort:** **S (4-6 hrs)** — gateway listens for SIGUSR1, drains, re-execs.

---

#### Tier 2.A.25 — `/debug` (upload debug report)

**What:** collect logs + config + session state, upload to a pastebin-like service, return a shareable link.

**Hermes:** `hermes_cli/commands.py:L169`

**OC state:** `[N/A]`. We don't have an upload server. Could pivot to "save debug report locally as .tar.gz" instead.

**Recommendation:** **Skip the upload variant; ship local-only debug bundle as `oc support-bundle` CLI.** S effort.

---

#### Tier 2.A.26 — `/<skill-name>` (invoke skill by name as slash)

**What:** any installed skill name becomes a slash command. `/pead-screener` invokes the PEAD screener skill.

**Hermes:** ubiquitous; `hermes_cli/commands.py`

**OC state:** `[partial]`. We have `/skill` (single command) but not auto-mapping all skill names to slashes.

**Effort:** **S (4-6 hrs)** — slash autocomplete already enumerates skills; just register dispatcher entries.

**Recommendation:** Port. Massive UX win for skill-heavy users.

---

### Tier 2.B — TUI feature parity

#### Tier 2.B.1 — `@filepath` mention autocomplete

**What:** type `@` in the input, get fuzzy file completion (mtime-sorted) for files in the workspace.

**Hermes:** `hermes_cli/completion.py`. v0.11.0 release notes call this out.

**OC state:** `[missing]`. We have slash autocomplete (PR #200) but no `@`-prefix completion.

**Effort:** **M (1.5 days)** — extend completer with `@` trigger; walk workspace, fuzzy-match, sort by mtime.

**Why it matters:** "Edit `@logs/error.log`" without typing the full path. Big win for coding-mode UX.

**Recommendation:** Port. M effort, daily-use win.

---

#### Tier 2.B.2 — External editor (`$EDITOR`) for multiline prompts

**What:** Ctrl-key opens `$EDITOR` (vim, nvim, code, etc.) to compose a long prompt; close editor → prompt is sent.

**Hermes:** v0.11.0 release notes.

**OC state:** `[missing]`.

**Effort:** **S (4 hrs)** — keybind opens `$EDITOR` on a temp file, reads back on save.

**Recommendation:** Port. Power-user staple.

---

#### Tier 2.B.3 — Worktree-per-session (`--worktree` / `-w`)

**What:** auto-create a git worktree at session start so the agent operates in an isolated branch. Session ends → worktree can be merged or discarded.

**Hermes:** `cli-config.yaml.example:L127`

**OC state:** `[missing]`.

**Effort:** **M (1.5 days)** — `--worktree` flag creates worktree at `<repo>/.opencomputer-worktrees/<session_id>/`, sets cwd, registers cleanup hook.

**Why it matters:** Safe experimentation in real codebases. "Let the agent rip in coding-harness, but on a worktree, so my main branch is untouched."

**Recommendation:** Port. M effort, safety win.

---

#### Tier 2.B.4 — Curses session browser parity check

**What:** Hermes ships `hermes sessions browse` — full curses UI with arrow-key nav, live search filter, title/preview columns, all in a TUI overlay.

**Hermes:** `hermes_cli/main.py:L333`

**OC state:** `[partial]`. Resume picker (PR #212) covers similar ground per the audit. Verify feature parity:
- Live search filter? Verify.
- Title + preview columns? Verify.
- Keyboard nav (vim-style j/k or arrow)? Verify.
- Pagination? Verify.

**Effort:** **S (verify) → M (gap-fill if needed, 1 day)**.

---

#### Tier 2.B.5 — Skin-aware spinner + thinking faces

**What:** each skin ships its own spinner braid (`waiting_faces`, `thinking_faces`, `thinking_verbs`, `wings`). Different vibe per skin.

**Hermes:** `cli.py:L72`, `hermes_cli/skin_engine.py`

**OC state:** `[missing]`. Generic spinner.

**Effort:** **S (3 hrs)** — bundled with Tier 2.A.5 skin engine.

**Recommendation:** Port together with skin engine.

---

#### Tier 2.B.6 — Bell on complete

**What:** terminal bell when long-running turn finishes. Configurable.

**Hermes:** `cli-config.yaml.example:L844`

**OC state:** `[missing]`.

**Effort:** **XS (15 min)** — emit `\a` on turn-complete event if config flag set.

---

#### Tier 2.B.7 — Background process notifications

**What:** when a background process the agent spawned (long bash command, long delegate task) completes/errors, raise a UI notification.

**Hermes:** `display.background_process_notifications: off|result|error|all`

**OC state:** `[missing]`. We have detached-task CLI but no in-TUI notification on completion.

**Effort:** **S (4-6 hrs)** — event bus already publishes task-state changes; subscribe in TUI, render bell + notification line.

---

#### Tier 2.B.8 — Token-streaming pacing for messaging platforms

**What:** `streaming.transport`, `edit_interval`, `buffer_threshold` for progressive message edits on Telegram/Slack/Discord.

**Hermes:** `cli-config.yaml.example:L458`

**OC state:** `[partial]`. Streaming is shipped per audit. Pacing config knobs may need adding.

**Effort:** **S-M (1 day)** — add `streaming.edit_interval_ms` to config, throttle edit-message API calls accordingly.

---

### Tier 2.C — Small misc

- **Reasoning effort levels** (`xhigh, high, medium, low, minimal, none`) — `[partial]`. We have on/off; level granularity missing. **S (2-3 hrs)** to wire to provider plugin.
- **`prefill_messages_file`** — load an ephemeral conversation seed from JSON. **S (3 hrs)**. Useful for per-task starting points.
- **`--ignore-rules`** / `--ignore-user-config` flags — bypass workspace + user config for this invocation. **S (2 hrs)**.

---

## Tier 3 — Operational hardening gaps

These are *infrastructure* gaps that don't show up in demos but matter when running 24/7 in production.

### Tier 3.A — Tirith pre-exec scanning

**What:** Hermes ships `tools/tirith_security.py` — an optional pre-execution scanner for terminal commands. Detects:
- Homograph URLs (cyrillic-look-alike domains)
- Pipe-to-shell patterns (`curl ... | bash`)
- Terminal injection (escape sequences in untrusted output)
- Env manipulation
- Auto-downloads with SHA-256 + cosign provenance

**Hermes pointers:** `tools/tirith_security.py`. Config flag `security.tirith_enabled`. `tirith_path`, `tirith_timeout`, `tirith_fail_open`.

**OC state:** `[partial]`. We have:
- URL safety (`security/url_safety.py`, PR #171) — IP-class blocking, private DNS probe, redirect re-validation.
- Path safety (no dedicated module per audit; relies on consent gate).
- Bash safety (`tools/bash_safety.py`).
- Instruction detector (PR #79) — prompt injection patterns.

**Missing:** homograph detection, pipe-to-shell pattern guard, terminal-escape-sequence sanitizer, provenance verification on auto-downloads.

**Why it matters:** Defense-in-depth for tool execution. Each missing pattern is a known attack class. The current consent layer catches "did we ask before running this," but Tirith catches "does running this *look* malicious before we even ask?"

**Effort:** **L (4-5 days)** — port the scanner with the four detection classes, wire into `BashTool.pre_call` hook, add config gate, add fail-open behavior.

**Recommendation:** **Port post-v1.0**, prioritize after Tier 1 + 2.A. Security depth.

---

### Tier 3.B — Runtime PII redaction (`privacy.redact_pii`)

**What:** strips phone numbers from logs/displays, replaces user/chat IDs with deterministic hashes (SHA-256 of `id+salt`), all *before* content reaches LLM context or display.

**Hermes:** `cli-config.yaml.example:L929`. Layered with display-time redaction.

**OC state:** `[partial]`. We have `evolution/redaction.py` (Round 2A P-14) but it's scoped to *trajectory export* — not runtime. Channel adapters do not run this filter.

**Why it matters:**
- Privacy audit `RR-3` and `RR-7` flag exposure paths.
- Phone numbers in Telegram contact-share, SMS, iMessage all currently land unredacted in logs.
- User/chat IDs are stable identifiers that link sessions to platform accounts — hashing them prevents reverse lookup.

**Effort:** **M (2-3 days)** — promote `evolution/redaction.py` to `opencomputer/security/pii_redactor.py`, expose via `PluginAPI`, register as `PRE_LLM_CALL` hook + display-layer filter, add `privacy.redact_pii: on/off` config.

**Recommendation:** **Port**. Plugs a privacy-audit flagged gap. M effort.

---

### Tier 3.C — Display-layer output redaction (`HERMES_REDACT_SECRETS`)

**What:** strips API keys, Bearer tokens, AWS access keys, etc. from streamed display output. Works at the rich.Console level — even if the model echoes a token, it never reaches the terminal.

**Hermes:** `agent/redact.py`. 7 regex patterns covering Bearer / xox-Slack / Telegram / Anthropic sk-ant- / OpenAI sk- / AWS AKIA / generic `secret_path`.

**OC state:** `[partial]`. Privacy audit `RR-4` notes credential pool logs *first 8 chars* of API key — survives existing redaction. No system-wide display-layer filter.

**Effort:** **S-M (1-2 days)** — add patterns, wrap rich.Console writes through filter, add env flag `OPENCOMPUTER_REDACT_SECRETS=on/off`.

**Coverage gaps to close while porting:**
- AWS secret key (40-char half) — flagged in audit
- GCP service-account JSON — flagged
- Generic JWTs — flagged

**Recommendation:** Port. S-M effort, plugs known privacy gap.

---

### Tier 3.D — More TTS providers (Edge TTS = FREE, KittenTTS local, more cloud)

**What:** Hermes ships **8 TTS backends:** Edge TTS (free, no API key), ElevenLabs, OpenAI TTS, MiniMax, Mistral Voxtral, Google Gemini TTS, xAI TTS, KittenTTS (local). Plus NeuTTS at `tools/neutts_synth.py`.

**OC state:** `[partial]`. We have OpenAI TTS via `voice/tts.py` + `tools/voice_synthesize.py` (PR #92, voice mode PR #199).

**Why this matters disproportionately:** **Edge TTS is free.** No API key. Decent voices. It's the obvious default for users who don't want yet-another-API-key. Adding it removes a friction point in onboarding.

**Effort per provider:**
- **Edge TTS:** S (1 day) — `pip install edge-tts`, add provider, register as default-when-available.
- **KittenTTS local:** S-M (1.5 days) — local model download, no API needed.
- **Google Gemini TTS:** S (1 day) — uses existing Google API key infra.
- **xAI TTS:** S (1 day) — uses existing XAI_API_KEY pattern.
- **MiniMax + Voxtral:** S each (1 day) — straightforward HTTP wrappers.

**Recommendation:** **Port Edge TTS first (free wins are free wins).** Then KittenTTS for offline. Then on-demand based on user platform preference.

---

### Tier 3.E — More STT providers (Groq cheap, Mistral Voxtral, xAI Grok STT)

**What:** Hermes ships 5 STT backends: local faster-whisper (configurable model), Groq (`GROQ_API_KEY`), OpenAI Whisper, Mistral Voxtral, xAI Grok STT.

**OC state:** `[partial]`. We have local Whisper fallback (mlx-whisper / whisper-cpp per voice-mode memory) + OpenAI Whisper.

**Why Groq matters:** Groq's Whisper-large-v3 is ~10x faster and ~5x cheaper than OpenAI's. For voice memo transcription on every Telegram audio message, costs add up.

**Effort:**
- **Groq:** S (1 day) — Groq is Whisper-API-compatible.
- **Mistral Voxtral:** S (1 day).
- **xAI Grok STT:** S (1 day).

**Recommendation:** Port Groq. Skip the others until demand surfaces.

---

### Tier 3.F — Hooks management CLI (`hooks list / test / revoke / clear`)

**What:** Hermes ships `hermes hooks [list|test|revoke|clear]` to manage shell hooks declared in `~/.hermes/config.yaml`. `test` dry-runs a hook. `revoke` removes a specific entry. `clear` wipes all.

**Hermes:** `hermes_cli/hooks.py`

**OC state:** `[partial]`. We have settings-based hooks (config.yaml `hooks:` key) shipped per CLAUDE.md III.6. We have plugin-declared hooks via `PluginAPI`. **No CLI surface for testing/listing/revoking individual hooks.**

**Effort:** **M (2 days)** — `oc hooks list` (read config + plugin-declared, render table), `oc hooks test <event> <matcher>` (run with synthetic context, show stdout/exit), `oc hooks revoke <id>` (delete from config.yaml with backup), `oc hooks clear` (with confirm).

**Why it matters:** Once hooks are common, debugging "why didn't my hook fire?" or "this hook is broken, kill it" without editing config.yaml manually is the difference between a feature and a *usable* feature.

**Recommendation:** Port. M effort, growing in importance as hook usage scales.

---

### Tier 3.G — `oc backup` + `oc import` (archive `~/.opencomputer/`)

**What:** `hermes backup` archives `~/.hermes` to a `.tar.gz`. `hermes import` restores. Includes config, memory, skills, sessions, audit chain.

**Hermes:** `hermes_cli/backup.py` + `hermes_cli/main.py:L7603`

**OC state:** `[missing]`. We have `oc profile rm` and `oc skills` etc. but no full-state archive.

**Effort:** **M (2 days)** — `oc backup` writes `.tar.gz` (excludes `.update_check.json`, lock files, ephemeral caches), `oc import` extracts to `~/.opencomputer/` after sanity checks (audit chain integrity, schema compat).

**Why it matters:**
- Disaster recovery (laptop dies → restore on new laptop in 30 sec).
- Privacy audit `RR-2` (sessions accumulating) — archive + clear pattern.
- Profile portability (different from profile clone — this is *full state*).

**Recommendation:** Port. M effort, infrastructure win.

---

### Tier 3.H — Profile clone / alias / export / import

**What:** Hermes profiles support:
- `--clone` — copy config + .env + SOUL.md from active profile
- `--clone-all` — copy entire `~/.hermes-<name>/` state
- `hermes profile alias` — create wrapper shell script (`myprofile-hermes` calls `hermes -p myprofile`)
- `hermes profile export` — `.tar.gz` of one profile
- `hermes profile import` — restore profile from archive

**Hermes:** `hermes_cli/profiles.py`, `hermes_cli/main.py:L8629-L8693`

**OC state:** `[partial]`. We have `oc profile list/create/use/delete/rename/path/deepening` (Phase 14.B). Missing: clone, alias-as-shell-wrapper, export, import.

**Effort:** **M (2-3 days)** for all four.

**Why it matters:** Multi-context users who run different profiles for different domains (work / personal / experimental). Clone-from-template is a major time-saver vs. "create blank, copy config, copy memory, copy skills, copy plugin enable list."

**Recommendation:** Port. M effort, broad UX value.

---

### Tier 3.I — Model aliases in config

**What:** in `config.yaml`, define short names that map to (model, provider, base_url) tuples:

```yaml
model_aliases:
  fast: { model: "claude-haiku-4-5", provider: "anthropic" }
  smart: { model: "claude-opus-4-7", provider: "anthropic" }
  cheap: { model: "deepseek-v3-1", provider: "openrouter", base_url: "..." }
```

Then `/model fast` switches.

**Hermes:** `cli-config.yaml.example:L902`

**OC state:** `[missing]`. We have `oc models list/register` (PR #120) but no alias system.

**Effort:** **S-M (1 day)** — add alias resolution to `/model` slash + `oc model` CLI.

**Recommendation:** Port. S-M effort, daily-use win for multi-model users.

---

### Tier 3.J — OpenRouter provider routing knobs

**What:** Hermes ships `provider_routing` config for OpenRouter:
- `sort: price|throughput|latency`
- `:nitro` shortcut (route through fastest available)
- `only`, `ignore`, `order` lists for upstream provider preference
- `require_parameters: true` (only use providers that support requested params)
- `data_collection: deny` (only use providers that don't log)

**Hermes:** `cli-config.yaml.example:L100`

**OC state:** `[missing]`. OpenAI-provider extension supports OpenRouter via base_url but doesn't pass routing hints.

**Effort:** **M (1.5 days)** — extend `extensions/openai-provider/` config schema, pass `provider` field through to OpenRouter API.

**Why it matters:** Users routing through OpenRouter today have no way to influence cost/latency/privacy beyond model selection.

**Recommendation:** Port. M effort, big OpenRouter user benefit.

---

### Tier 3.K — Session reset auto-policy

**What:** `session_reset.mode: both|idle|daily|none`. With:
- `idle`: reset after `idle_minutes` of no input
- `daily`: reset at `at_hour` daily
- `both`: whichever fires first
- `none`: manual only

**Hermes:** `cli-config.yaml.example:L441`

**OC state:** `[missing]`. We have `/reset` and `/new` slash but no auto-policy.

**Effort:** **S-M (1 day)** — gateway daemon polls config, fires reset event when triggered.

**Why it matters:** Long-running gateway sessions accumulating noise. Auto-reset hygiene.

**Recommendation:** Port. S-M effort.

---

### Tier 3.L — Auto-prune old sessions at startup

**What:** at startup, prune `state.db` rows older than `session_retention_days` (default 90).

**Hermes:** v0.11.0 release notes.

**OC state:** `[missing]`. Privacy audit `RR-2` flags this. SessionDB grows unbounded.

**Effort:** **S (4-6 hrs)** — startup hook calls `SessionDB.prune(older_than_days=90)`, configurable.

**Recommendation:** Port. S effort, plugs privacy audit gap.

---

### Tier 3.M — Response pacing (`human_delay`)

**What:** modes `off|natural|custom` with min/max ms. Inserts human-like pauses between message chunks on messaging platforms. Anti-bot pattern (some platforms throttle bots that respond in <500ms).

**Hermes:** `HERMES_HUMAN_DELAY_MODE`, `cli-config.yaml.example:L324`

**OC state:** `[missing]`.

**Effort:** **S (4 hrs)** — random sleep between dispatch and send.

**Why it matters:** WhatsApp/Telegram occasionally rate-limit "instant-response" bots. Also: some users find sub-second replies disorienting.

**Recommendation:** Port. S effort.

---

### Tier 3.N — `error_classifier` (typed error taxonomy)

**What:** centralized classifier returning typed enums for 401/429/5xx/timeout/network/quota/etc. Enables provider-aware retry logic.

**Hermes:** `agent/error_classifier.py`

**OC state:** `[missing]`. Loop captures raw `type(exception).__name__` (PR T3.1) in trajectory metadata. No typed taxonomy.

**Effort:** **M (2 days)** — port classifier with ~10 categories, wire into provider plugins, retry_utils.

**Recommendation:** Port. M effort, improves retry reliability across providers.

---

### Tier 3.O — `usage_pricing` per-call cost auto-compute

**What:** loop computes per-response `cost_usd` from `model_metadata` (input/output cost per Mtok) × usage. Currently providers must pass cost in.

**Hermes:** `agent/usage_pricing.py`

**OC state:** `[partial]`. CostGuard accepts `cost_usd` but callers compute. `model_metadata` has the cost fields.

**Effort:** **S-M (1 day)** — wire compute into AgentLoop; provider plugins return raw token counts; loop attaches cost.

**Recommendation:** Port. S-M effort, simplifies provider plugin contract.

---

### Tier 3.P — `retry_utils` shared utility (tenacity-backed)

**What:** centralized exponential-backoff retry decorator. Used by channel send, provider call, MCP call, etc.

**Hermes:** `agent/retry_utils.py`

**OC state:** `[partial]`. CredentialPool has `with_retry`; evolution storage has `_with_retry`; channel spec §4.5 will add `_send_with_retry`. No shared utility.

**Effort:** **S (1 day)** — port wrapper, refactor 3 callers.

**Recommendation:** Port. Minor consolidation win.

---

### Tier 3.Q — `redact` shared utility

**What:** central `redact(text: str) -> str` for PII/secrets, callable by any code path.

**Hermes:** `agent/redact.py`

**OC state:** `[partial]`. evolution/redaction.py is one path; skills extractor has inline patterns; no shared util.

**Effort:** **S (1 day)** — promote evolution/redaction.py to `opencomputer/security/redact.py`, add helper to PluginAPI.

**Bundles with Tier 3.B + 3.C.**

---

### Tier 3.R — Cron per-job toolset scoping + `wakeAgent` gate

**What:**
- Per-job `enabled_toolsets`: cap which tools a cron job can use (cap context overhead).
- `wakeAgent` gate: scripts can skip the agent entirely for lightweight scheduled tasks (e.g., a rotation script that doesn't need LLM at all).

**Hermes:** `cli-config.yaml.example:L265`. v0.11.0 release notes.

**OC state:** `[partial]`. Cron shipped (PR #85). Per-job toolset gating not in current schema.

**Effort:** **S-M (1 day)** — add `toolsets: [...]` to cron job entry; AgentLoop respects on cron-triggered turn.

**Why it matters:** Privacy audit `RR-7` flags cron-fired turns as bypassing interactive consent. Per-job toolset gates *bound* the bypass.

**Recommendation:** Port. S-M effort, plugs audit gap.

---

### Tier 3.S — Honcho overhaul features (5-tool surface, cost safety, session isolation)

**What:** Hermes v0.11.0 overhauled Honcho with:
- 5-tool surface (instead of monolithic memory_tool)
- Cost safety (Honcho calls counted against budget)
- Session isolation (cross-session leakage prevention)

**Hermes:** `plugins/memory/honcho/session.py`

**OC state:** `[shipped]` — Sub-project A made Honcho the default per CLAUDE.md. **Verify** the 5-tool / cost-safety / session-isolation features are at parity.

**Effort:** **Audit (S, 4 hrs)** — read our Honcho plugin, compare against Hermes's overhaul, file follow-ups for any gaps.

---

### Tier 3.T — `oc update` imperative subcommand

**What:** explicit "upgrade me now" command. Hermes does `git pull + uv pip install -e ".[all]"` + clear `__pycache__`.

**Hermes:** `hermes_cli/main.py`

**OC state:** `[partial]`. PR #147 added background prefetch + chat-start hint, but no imperative subcommand.

**Effort:** **S (4 hrs)** — `oc update` wraps `pip install --upgrade opencomputer` (or git pull + reinstall in dev mode), clears caches, prints version delta.

**Recommendation:** Port. S effort, improves DX.

---

### Tier 3.U — `oc completion [bash|zsh|fish]` shell completion

**What:** print shell completion script for installed shell.

**Hermes:** `hermes_cli/main.py:L8717`

**OC state:** `[missing]` (verify — Typer has completion built-in, may already work).

**Effort:** **XS (2 hrs)** — Typer's `--install-completion` flag may already cover this. Verify.

---

### Tier 3.V — `oc uninstall [--full]`

**What:** uninstall command. `--full` removes `~/.opencomputer/` too.

**Hermes:** `hermes_cli/uninstall.py`

**OC state:** `[missing]`.

**Effort:** **S (4 hrs)**.

---

### Tier 3.W — File hardening (~/.opencomputer to 0700, .env to 0600)

**What:** Hermes sets `~/.hermes/` mode to 0700 and `.env` to 0600 at write time.

**Hermes:** `hermes_cli/config.py:L229`

**OC state:** `[partial]` (verify). If we don't, this is a 30-min fix flagged by privacy audit philosophy.

**Effort:** **XS (30 min)**.

**Recommendation:** Port. Trivial security baseline.

---

### Tier 3.X — Subagent depth limit verification

**What:** Hermes hardcodes `MAX_DEPTH = 2` for delegate to prevent recursive runaway.

**Hermes:** `SECURITY.md:L44`. `tools/delegate_tool.py`.

**OC state:** `[shipped]`. PR #75 added MAX_DEPTH + DELEGATE_BLOCKED_TOOLS. **Verify** value matches Hermes (2) and blocked-tools list is at parity.

**Effort:** **Audit (XS, 30 min)**.

---

### Tier 3.Y — `prompt injection scan` for context files (AGENTS.md / CLAUDE.md / .cursorrules / SOUL.md)

**What:** scan context files for injection patterns *before* injecting into system prompt.

**Hermes:** `agent/prompt_builder.py:L55`

**OC state:** `[partial]`. We have `InstructionDetector` (PR #79) for runtime; verify it runs on context-file load.

**Effort:** **S (4 hrs)** — wire InstructionDetector into PromptBuilder context-file load path.

---

## Tier 4 — Plugin ecosystem expansion

### Tier 4.A — More bundled memory backends (6 missing)

**What:** Hermes ships 7 memory backends: Honcho, OpenViking, Mem0, Hindsight, Holographic, RetainDB, Byterover.

**OC state:** `[partial]`. Honcho only.

**Inventory verdict (2026-04-22):** "skip" — port the ABC, leave specific backends to user.

**Re-evaluation (2026-04-28):** verdict still defensible. Specific backends are L-effort each (3-7 days) and the audience for "switch from Honcho to Mem0" is small. Mem0 is the only one with genuine differentiation (large-context optimization, free tier).

**Recommendation:** **Defer until user demand.** Track usage signal — if a Honcho-related friction surfaces, add Mem0 as alternative (M effort).

---

### Tier 4.B — Edge TTS + Groq STT (free / cheap providers)

**Already covered in Tier 3.D + 3.E.** Listed here as a plugin-bundling story:
- Edge TTS as `extensions/voice-edge/` (free, no API key, default-when-available)
- Groq STT as augment to existing `extensions/voice-mode/`

**Recommendation:** Port. Free-tier wins for onboarding.

---

### Tier 4.C — Browser providers (Browserbase / Firecrawl / Camofox)

**What:** Hermes lets browser tools route through different providers:
- **Browserbase** (cloud, residential proxies, advanced stealth) — $$
- **Firecrawl** (web scraping focus) — $
- **Camofox** (anti-fingerprint browser) — local, free
- **Browser Use** (LLM-driven browser library) — free
- **Local CDP** (attach to running Chrome) — free, see Tier 2.A.23

**OC state:** `[partial]`. browser-control extension (PR #202) uses Playwright local. No multi-provider story.

**Effort:**
- **Browserbase:** S-M (1.5 days) — accept `browser_provider: browserbase` config, swap launch with cloud connect.
- **Firecrawl:** S (1 day).
- **Camofox:** S-M (1.5 days) — niche; defer until anti-fingerprinting demand.
- **Local CDP attach:** M (2 days) — see Tier 2.A.23.

**Recommendation:** Port **Local CDP first** (free, big workflow win). Browserbase/Firecrawl on demand.

---

### Tier 4.D — Browser CDP + Console tools

**What:** in addition to navigate/click/fill, Hermes ships:
- `browser_cdp` — raw CDP passthrough (power-user escape hatch)
- `browser_console` — read browser JS console
- `browser_get_images` — extract images from current page
- `browser_vision` — vision analysis of screenshot
- `browser_press` — keyboard shortcut
- `browser_back` — history nav

**OC state:** `[partial]`. PR #202 ships 5 tools (navigate/click/fill/snapshot/scrape). Missing: cdp, console, get_images, vision, press, back.

**Effort:** **S each (4-6 hrs)** — straightforward Playwright wrappers.

**Recommendation:** Port `browser_console` and `browser_back` first (most-used). `browser_cdp` for power users. Skip `browser_get_images`/`browser_vision` until vision tool is bundled (Tier 1.B).

---

### Tier 4.E — Discord-as-tool (agent-side)

**What:** Hermes ships `tools/discord_tool.py` so the agent can interact with Discord servers (read messages, post to channels, manage roles). *Different from* the Discord channel adapter.

**OC state:** `[missing]`.

**Effort:** **M (2 days)** — agent-side tool wraps Discord API; respects `DISCORD_BOT_TOKEN`.

**Recommendation:** Port if Discord-centric users surface. Defer otherwise.

---

### Tier 4.F — Home Assistant tools (`ha_*`)

**What:** Hermes ships 4 HA tools — `ha_list_entities`, `ha_get_state`, `ha_list_services`, `ha_call_service` — agent-side to control HA from any session.

**OC state:** `[partial]`. We have HA *channel adapter* (extensions/homeassistant/). Tools side missing.

**Effort:** **M (2 days)** — port 4 tools, share auth with channel adapter.

**Recommendation:** Port if HA-active users surface. M effort, niche.

---

### Tier 4.G — Disk-cleanup plugin

**What:** Hermes ships `plugins/disk-cleanup/` (opt-in by default) for cleaning the agent's workspace caches.

**OC state:** `[missing]`.

**Effort:** **S (4 hrs)** — port shape; identify our cache directories (tool result spillover, session DB, audit chain rotated logs, BGE model cache, voice cache).

**Recommendation:** Port. S effort, hygiene win.

---

### Tier 4.H — `acp_registry` multi-IDE discovery

**What:** Hermes's `acp_registry/agent.json` declares an Agent Client Protocol manifest for IDE auto-discovery. Multiple IDEs (VS Code, Zed, JetBrains, Cursor) can find and connect.

**OC state:** `[partial]`. We have ACP server (PR #102). Missing the registry-side auto-discovery story.

**Effort:** **M (2 days)** — write `acp_registry/agent.json`, document IDE setup, test against at least Zed.

**Recommendation:** Port if any user surfaces an IDE-integration request.

---

## Tier 5 — Personalities / soul / identity

This is its own category because it's mostly **bikeshedding** with surprisingly large reach.

### Tier 5.A — More built-in personalities (flat list)

**What:** Hermes config ships 14 named flat personalities — `helpful, concise, technical, creative, teacher, kawaii, catgirl, pirate, shakespeare, surfer, noir, uwu, philosopher, hype`.

**OC state:** `[partial]` with different model. We have **6 personas** (`admin, coding, companion, learning, relaxed, trading`) which are *context-based* (active persona depends on detected user state). Hermes's personalities are *role-based* prompt overlays the user picks.

**The mismatch:** different concept. Both useful.

**Effort:** **S (1 day)** — add a flat `personalities` section to config.yaml with prompt overlays; new `/personality` slash separate from `/persona`. Bundle 5-6 starter personalities (helpful, concise, teacher, hype, philosopher — skip `catgirl`/`uwu`/`kawaii` unless user wants).

**Recommendation:** Port. S effort, complementary to plural personas.

---

### Tier 5.B — SOUL.md identity anchor

**What:** Hermes loads `~/.hermes/SOUL.md` *fresh on every message* (not cached) so identity edits take effect immediately. Default seed at `hermes_cli/default_soul.py`.

**OC state:** `[shipped]`. Sub-project C (PR #24) established SOUL.md per-profile. Verify load-fresh-every-message behavior matches.

**Effort:** **Audit (XS, 30 min)**.

---

### Tier 5.C — Companion persona / mood thread / vibe classifier

**OpenComputer-only.** Hermes has nothing equivalent. PRs #188-#191 (V2.C). No gap; gap in *the other direction*.

---

## Tier 6 — Cross-cutting plumbing gaps

### Tier 6.A — `error_classifier` typed taxonomy — see Tier 3.N

### Tier 6.B — `usage_pricing` per-call cost auto-compute — see Tier 3.O

### Tier 6.C — `retry_utils` shared utility — see Tier 3.P

### Tier 6.D — `redact` shared utility — see Tier 3.Q + 3.B + 3.C

### Tier 6.E — `acp_registry` multi-IDE discovery — see Tier 4.H

### Tier 6.F — Tirith pre-exec — see Tier 3.A

### Tier 6.G — `path_security` module

**What:** Hermes ships `tools/path_security.py` with `WRITE_DENIED_PATHS` traversal guard.

**OC state:** `[partial]`. We rely on consent-gate capability claims and sandbox strategy for path-class blocking. No dedicated module.

**Effort:** **S-M (1 day)** — port DENY-list patterns (e.g., `/etc/`, `/System/`, `/usr/bin/`, `~/.ssh/`, `~/.aws/`), wire into Edit/Write/MultiEdit pre-call hook.

**Recommendation:** Port. S-M effort, security depth.

---

### Tier 6.H — `file_safety` module

**What:** `agent/file_safety.py` — file-operation safety guards (size, permission, type checks).

**OC state:** `[partial]`. Some checks scattered.

**Effort:** **S (1 day)** — consolidate into `opencomputer/security/file_safety.py`.

---

### Tier 6.I — Network IPv4 preference

**What:** `network.force_ipv4: true` config — applies early, useful in IPv6-broken networks.

**Hermes:** `hermes_cli/main.py:L179`

**OC state:** `[missing]`.

**Effort:** **XS (30 min)** — `socket.AF_INET` override at startup if config set.

---

### Tier 6.J — `HERMES_SIGTERM_GRACE` graceful shutdown

**What:** env var sets grace period on SIGTERM before SIGKILL. Useful in containers.

**OC state:** `[missing]` — verify.

**Effort:** **S (3 hrs)**.

---

### Tier 6.K — NixOS / `.container-mode` marker

**What:** marker file routes `hermes` commands into managed container. Niche.

**OC state:** `[missing]`. Niche.

**Effort:** **M (2 days)**. Skip until NixOS user surfaces.

---

### Tier 6.L — Skills external_dirs

**What:** `skills.external_dirs: [...]` lists directories of read-only shared skill sets — e.g., team-managed skill repo.

**OC state:** `[missing]` — verify.

**Effort:** **S (4 hrs)** — extend skills loader to walk external dirs.

**Recommendation:** Port. Useful for shared-team setups.

---

## Tier 7 — Already won't-do (per CLAUDE.md §5)

**Listed here for completeness. Do NOT propose porting unless explicitly reopened.**

- **Wake-word voice activation** (Hermes does have wake words on macOS/iOS) — wont-do
- **Atropos RL training integration** — wont-do
- **Trajectory compression** — wont-do (we have trajectory export only, which is Tier S)
- **Daytona / Singularity / Modal terminal backends** — wont-do (we have local/docker/ssh/macOS-sandbox/linux-bwrap; the cloud-serverless ones are deferred)
- **Skills marketplace (full)** — wont-do at full scope. Note: **Tier 1.A above proposes a *minimal* skills hub** (browse/install/publish/tap/audit). That's not a marketplace. Tier 7 forbids the *full* "skill economy with payments + ratings + curation" surface.
- **Full i18n** — wont-do (English only ships; Chinese/Japanese deferred)
- **Native mobile apps** — wont-do
- **Canvas rendering** — wont-do (this is an OpenClaw feature; not in Hermes)

If any of these reopen, the audit pointer is here.

---

## Tier 8 — Deliberate skips (different positioning)

**Not gaps because we shouldn't have them.**

### Tier 8.A — Nous Portal OAuth (`hermes login` / `logout`)

Hermes integrates with Nous Research's paid API tier (Nous Portal, gated tools). OC is provider-neutral. **Skip.**

### Tier 8.B — `hermes claw migrate`

Hermes's import-from-OpenClaw command. We have a parallel-ish path via `oc preset` and our channel-port spec. **Skip the literal command name; document our equivalent.**

### Tier 8.C — Asia-region channels

Per channel-port spec §2: Feishu, DingTalk, WeCom, Weixin, QQ, Zalo, QQBot — **deliberate-skip** (~12k LOC; geographic/language mismatch). Reopen only with concrete user demand.

### Tier 8.D — BlueBubbles iMessage

Per Track-B audit: imessage shipped. Verify whether it uses BlueBubbles. If yes, no gap.

### Tier 8.E — `/gquota` (Google Gemini Code Assist quota)

Provider-specific. Skip until Gemini provider plugin exists.

### Tier 8.F — Tinker-Atropos training submodule

Same as Tier 7 Atropos. Skip.

### Tier 8.G — Hermes web dashboard plugin examples (`strike-freedom-cockpit`)

These are Hermes-branded gimmicks. Don't port literally. The dashboard plugin *system* is Tier 1.C.

### Tier 8.H — `hermes debug share` (upload to Hermes server)

We don't run a server. Tier 2.A.25 proposes a local-only variant.

### Tier 8.I — HermesClaw community WeChat bridge

Niche. Skip.

---

## Top-5 highest-impact picks (recap, with one-paragraph rationale each)

If you can only port five things from this entire doc:

### 1. Skills Hub network layer + agentskills.io standard (Tier 1.A + 1.D)
**Why:** Largest visible-to-user gap. Most strategic. Skills-as-network-effect is the single feature that punches above its weight in marketing demos and in long-term ecosystem viability. **L → XL effort. ~5-10 days for MVP. Defer the publish/tap/snapshot extras to a follow-up.**

### 2. First-class generative tool registrations (Tier 1.B + 1.E)
**Why:** Closes the "OC has the capability but the agent doesn't reach for it" gap. Biggest invisible-to-feature-list win for everyday workflows. **S+S+S+M = 7-10 days for all four (`SendMessage` first, `VisionAnalyze` second, `ImageGenerate` third, `MoA` fourth).**

### 3. Slash command bundle (Tier 2.A.1-2.A.26)
**Why:** ~10 small individual wins that collectively reshape the in-loop experience. `/branch`, `/btw`, `/snapshot`, `/copy`, `/queue`, `/reasoning`, `/fast`, `/skin` are the headline ones. Most are S effort; can parallelize. **Total ~10 dev-days for all 26 slashes.**

### 4. Edge TTS (free) + Groq STT (cheap) + auto-prune sessions (Tier 3.D + 3.E + 3.L)
**Why:** Edge TTS removes API-key friction on TTS forever. Groq STT cuts voice-memo-transcription cost by ~5x. Auto-prune plugs privacy audit `RR-2`. All three are S effort. **Total ~3 dev-days.** Free wins are free wins.

### 5. `oc backup` + profile clone/alias/export/import (Tier 3.G + 3.H)
**Why:** Disaster recovery + multi-context users. Tier-3 infrastructure that becomes invisible-but-critical the first time someone's laptop dies. Profile clone is the single biggest DX improvement for users juggling work/personal/experimental contexts. **M+M = 4-5 dev-days.**

---

## What this audit deliberately omits

- **OpenClaw deep-dive** — separate companion gap doc, scheduled on user signal. OpenClaw has unique surface (multi-agent routing fabric, `nodes`/`canvas`/`A2UI`/`sessions_*` as first-class tools, sandbox tier defaults, 25+ channels) that overlaps with both Hermes and OC in interesting ways.
- **Kimi CLI gap audit** — already covered by `docs/refs/kimi-cli/` extraction notes per CLAUDE.md §9.
- **Claude Code gap audit** — Sub-project D's coding-harness ports cover most of this.
- **OC features that Hermes lacks** — see `docs/audit/05-skills-tools-plugins.md` and the awareness section in this audit's parity-pass agent output. Examples: Layered Awareness L0-L4, Life-Event Detector, vibe classifier, ambient foreground sensor, F1 consent layer with HMAC audit chain, F4 user-model graph, F5 decay/drift, instruction detector, OI removal, plural personas with classifier, auto-skill-evolution with quarantine→approve, settings-based hooks (Claude-Code compatible), `oc plugin new` scaffolder, demand-driven plugin activation, per-profile dirs + workspace overlay, settings variants, HMAC audit chain, learning moments, affect injection, native cross-platform introspection, ambient sensors phase 1, browser bridge for Layer 4.
- **Implementation specifics** — port-design details for each of the above gaps will be in their respective implementation specs (`docs/superpowers/specs/...`) when scheduled. This doc is the *what + why + size* layer.

---

## Sources & cross-references

- **Hermes upstream mirror:** `/Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/`
- **Hermes public docs:** `https://hermes-agent.nousresearch.com/docs/` (Quickstart, CLI, Configuration, Messaging Gateway, Security, Tools, Skills, Memory, MCP, Cron, Context Files, Architecture, CLI Reference, Environment Variables)
- **Existing port spec:** `docs/superpowers/specs/2026-04-28-hermes-channel-feature-port-design.md`
- **Existing inventory:** `docs/refs/hermes-agent/inventory.md` (2026-04-22)
- **Privacy audit:** `docs/audit/06-privacy.md` (2026-04-27)
- **Skills/tools/plugins audit:** `docs/audit/05-skills-tools-plugins.md` (2026-04-27)
- **Platform-reach guide:** `docs/superpowers/specs/2026-04-27-platform-reach-port-guide.md`
- **CLAUDE.md ground truth:** `OpenComputer/CLAUDE.md` (2026-04-24, post-Sub-projects-A/B/C/D/E)
- **Memory entries cross-checked:**
  - `project_track_b_already_shipped.md` (9 channel adapters in extensions/)
  - `project_tier_s_port_done.md` (PR #171 28-item Hermes port)
  - `project_oi_removal_native_introspection_done.md` (PR #179)
  - `project_voice_mode_done.md` (PR #199)
  - `project_browser_control_done.md` (PR #202)
  - `project_t2_batch_skills_done.md` (PR #203)
  - `project_skill_evo_session_adapter_done.md` (PR #204)
  - `project_layered_awareness_mvp_done.md`, `_v2b_done.md`, `_v2c_done.md`
  - `project_ambient_foreground_sensor_done.md` (PR #184)
  - `project_auto_skill_evolution_done.md` (PR #193)
  - `project_opencomputer_tui_phase1.md` (PR #180; phase 2 = #200)
  - `feedback_subagent_followup_handoff.md` (silent-handoff guard rule)

---

## Status & next steps

**This document:** Tier-1 gap analysis, designed to be picked from.

**Recommended sequencing:**
1. **Wait for the dogfood-gate window to close** (CLAUDE.md §5 names this as load-bearing).
2. **Tag v1.0** + PyPI release (CLAUDE.md §5 Tier 1).
3. **Then:** OpenClaw companion gap doc on user signal.
4. **Then:** pick from this doc's Top-5 based on which gap signals strongest.
5. **Tier 1.A (Skills Hub) is the strongest single bet** if no specific signal surfaces.

**Owner:** TBD per gap.

**Last updated:** 2026-04-28.

**End of audit.**
