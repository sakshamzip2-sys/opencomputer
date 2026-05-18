# OpenComputer — "Take the best from Hermes + Claude Code + OpenClaw" — AUDIT

**Date:** 2026-05-17
**Companion file:** `2026-05-17-best-of-three-port-plan.md` (the recipes).
**Sources audited:**
- Claude Code — `sources/claude-code/`, official docs at `code.claude.com/docs`
- Hermes Agent — `sources/hermes-agent/`, official docs at `hermes-agent.nousresearch.com/docs`
- OpenClaw — `sources/openclaw/`, official docs at `docs.openclaw.ai`
- MCP spec — `modelcontextprotocol.io/specification/2025-06-18`
**OC ground truth:** main at HEAD = `3849a7eb` (today), verified by direct file reads.

---

## Reading rules

1. Every claim cites a file. If the file doesn't say what I claim, the claim is wrong — file an issue.
2. Three status states: **SHIPPED** (verified in tree), **PARTIAL** (scaffold exists, not wired or under-scoped), **MISSING** (no code at all).
3. The companion file has the port recipes. This file is "what + why," not "how."

---

## Section 1 — Component types (the slot model)

A "component type" is a first-class concept the plugin manifest can declare. Claude Code formalized this most cleanly. Per `code.claude.com/docs/en/plugins-reference`, a Claude Code plugin manifest declares zero or more of: `skills`, `agents`, `hooks`, `commands`, `mcp_servers`, `lsp_servers`, `monitors`, `themes`, `output_styles`, `statusline`.

OC's status against that ten-slot model:

| Slot | What it does | Claude Code | Hermes | OpenClaw | OC |
|---|---|---|---|---|---|
| **skills** | `SKILL.md` files agent loads on demand | ✓ | ✓ | ✓ | ✓ shipped — `opencomputer/skills/` + skills hub |
| **agents** | Pre-canned subagent templates (model + tools + system prompt in markdown) | ✓ | – | ✓ | partial — `opencomputer/agents/{code-reviewer,explore,general-purpose,plan}.md` (only 4, no per-plugin agents) |
| **hooks** | Lifecycle event handlers | ✓ (~20 events) | ✓ | ✓ | ✓ ahead — 25+ events at `plugin_sdk/hooks.py::HookEvent` |
| **commands** | User-authored markdown slash commands (drop `.md` → get `/foo`) | ✓ | ✓ (skill commands) | ✓ | **MISSING** — slash commands are Python only |
| **mcp_servers** | stdio/HTTP MCP server entries | ✓ | ✓ | ✓ | ✓ shipped — `opencomputer/mcp/` (PR #612) |
| **lsp_servers** | Plugin contributes an LSP server | ✓ (component type) | – | – | partial — `extensions/lsp-bridge` is one bundled plugin, not a component slot |
| **monitors** | Background watchers (file/git/timer/PID) | ✓ | – | – | **MISSING** — no concept |
| **themes** | Pure visual override, no behavior | ✓ | ✓ (skin engine, 8 builtins) | – | **MISSING** — wordmark hex hardcoded, no engine |
| **output_styles** | Plugin overrides response formatting (explanatory, learning, executive) | ✓ | – | – | **MISSING** |
| **statusline** | Plugin renders persistent bottom row | ✓ | partial | – | **MISSING** |

**Three of those slots matter strategically:** `commands` (user-authored markdown), `themes` (skin engine), `output_styles` (response format plugins). The others are either already shipped (skills, hooks, mcp_servers) or niche (monitors, lsp_servers).

---

## Section 2 — Lifecycle / runtime mechanisms

How the host actually loads + manages plugins. Different from "what slots exist" — this is "what happens when a plugin gets loaded."

| Mechanism | Source repo (best impl) | OC status | Note |
|---|---|---|---|
| **In-process `register(api)` loading** | all three converge | ✓ shipped — `opencomputer/plugins/loader.py` | Plugin code runs in host process, not subprocess. Industry standard. |
| **Two-phase manifest-first discovery** | OpenClaw | ✓ shipped — `opencomputer/plugins/discovery.py` + `loader.py` | Phase 1 = cheap manifest scan, Phase 2 = lazy import. Already correct. |
| **Manifest-first ACTIVATION planner** | OpenClaw | **PARTIAL — code exists, not wired** | `opencomputer/plugins/activation_planner.py:47` defines `plan_activations()`, but grep proves it's never called from `cli.py` or `agent/loop.py`. With 86 extensions, cold start loads everything. Real performance bug. |
| **Install security scan + signed catalogs** | Hermes (Tirith), Claude Code | ✓ ahead — `opencomputer/plugins/install_security_scan.py`, `integrity.py`, `sigstore_verify.py`, `catalog_signing.py` | OC is materially ahead. |
| **Plugin install index** | Hermes, OpenClaw | ✓ shipped — `opencomputer/plugins/installed_index.py` | Records source URL + sha for re-verification. |
| **Source policy (allow/deny per source type)** | OpenClaw | ✓ shipped — `opencomputer/plugins/source_policy.py` | Per-source allow/deny for `pypi/github/git/url/directory`. |
| **Plugin demand tracker** | OC-original | ✓ shipped — `opencomputer/plugins/demand_tracker.py` | When LLM calls an unregistered tool, log which disabled plugin would have provided it. Hermes-style "enable suggestion" UX. |
| **Hot-reload** | partial in Hermes (`reload-skills`) | **MISSING** — only `reload-mcp` works | Edit a plugin's `plugin.py`, see changes without restart. Critical for plugin authors. |
| **Per-plugin doctor** | Hermes (per-plugin) | **PARTIAL** — `cli_plugin_scaffold.py` validates scaffolds; no `oc plugins doctor <id>` | Diagnose missing deps / blocked-by-policy / failed `register()` for installed plugins. |
| **MCP stdio subprocess lifecycle** | spec-standard | ✓ shipped — `opencomputer/mcp/` (PR #612, landed) | Cross-task shutdown wall + non-blocking startup + force-kill on hang. |
| **Hook fire-and-forget timeout** | Hermes | ✓ shipped — `opencomputer/hooks/engine.py` | Fail-open on timeout (PR #633). |
| **Plugin command aliases in manifest** | OpenClaw (`PluginManifestCommandAlias`) | **MISSING** — aliases live in Python `slash.py` | Hardcoded means plugin can't declare its own aliases. |
| **Capability-typed registration** | OpenClaw (`PluginManifestContracts`) | partial — `register(api)` is loose | OpenClaw's strict typing lets a plugin declare "I provide a provider" and get a typed registration arg back. OC's `api` is duck-typed. |
| **Plugin process isolation (sandboxed)** | none of the three | none | All four agents load native plugins in-process. Genuine industry gap, not OC-specific. |

**Hot take**: OC's loader is structurally on par with Hermes and ahead of Claude Code on safety (signed catalogs, source policy, install scan). The only real LIFECYCLE gap is **wiring `activation_planner.py` into the actual load pipeline** — the code exists, just nothing calls it.

---

## Section 3 — Ecosystem plumbing

The "outside the host" surface — marketplaces, distribution, sharing.

| Thing | Best impl | OC status |
|---|---|---|
| **Plugin marketplaces (plural, named)** | Claude Code — `plugin marketplace add owner/repo` | **MISSING** — `opencomputer/plugins/remote_install.py` exists but only ONE hardcoded catalog URL |
| **Browsable marketplace UI** (`oc plugin browse`, `oc plugin search`) | Hermes (agentskills.io) + Claude Code | **MISSING** for plugins; **shipped for skills** at `opencomputer/skills_hub/` |
| **Plugin tags / categories** | Claude Code (`plugin tag`) | MISSING |
| **Per-marketplace trust levels** | Claude Code | MISSING — single signing key, single source |
| **Plugin update notifier** | Hermes | MISSING — `cli_update_check.py` is for OC itself, not for installed plugins |
| **`plugin enable` / `plugin disable` (toggle without uninstall)** | Hermes (`plugins.enabled` list) | partial — `plugin_sources.yaml` deny works, no clean toggle UX |
| **Plugin docs site auto-generated from manifests** | Hermes | MISSING |
| **Agentskills.io standard compatibility** | Hermes | partial — `skills_hub/agentskills_validator.py` exists, no live cross-agent install |
| **OpenClaw ClawHub-style multi-source skill registry** | OpenClaw + Hermes both have similar | ✓ shipped for skills (`opencomputer/skills_hub/sources/`) — gap is for plugins, not skills |

**Hot take**: For SKILLS, OC is on par. For PLUGINS, OC has a single hardcoded marketplace. The "plural named marketplaces" feature from Claude Code is the most ergonomic. ~3-4 days work.

---

## Section 4 — Slash commands

OC: 38 in `opencomputer/cli_ui/slash.py`. Hermes: 61 in `sources/hermes-agent/hermes_cli/commands.py`. Claude Code: ~25 built-in + unlimited user-authored markdown.

**OC's actual implementations are at `opencomputer/agent/slash_commands_impl/` — 40 files.** Several exist as code but aren't registered in `slash.py` yet (drift between impl and registry). Examples confirmed by grep:

- `background_cmd.py`, `btw_cmd.py` → no `CommandDef` registered
- `agents_cmd.py` → no `CommandDef` registered
- `copy_cmd.py` → no `CommandDef` registered
- `rollback_cmd.py`, `restore_cmd.py` → no `CommandDef` registered
- `history_cmd.py` → no `CommandDef` registered
- `save_cmd.py`, `title_cmd.py` → no `CommandDef` registered

**This is a different gap than I called out in the April 28 audit.** Those commands aren't missing — they're written but not wired. Recipe in the port plan: ~2 hours to register them all.

**Commands Hermes has and OC genuinely lacks** (verified):

| Hermes command | Purpose | Why it matters |
|---|---|---|
| `/fast` | toggle Anthropic priority / OpenAI fast mode | Real cost knob |
| `/curator` | background skill maintenance | We have skill-evolution; need surface |
| `/commands` | paginated command browser | UX for the 40+ commands once registered |
| `/personality` | persona swap | We have personas; rename `/skin` → `/persona` |
| `/verbose` | cycle tool-output verbosity (off/new/all/verbose) | Power-user knob |
| `/redraw` | re-render screen | Tiny but useful when terminal corrupts |
| `/restart` | restart CLI keeping config | Recovery without losing config |
| `/yolo` | dangerous-auto-approve toggle | DEFER — OC's consent gate is stricter by design |

**Claude Code's killer feature**: user drops `~/.claude/commands/deploy.md` and it becomes `/deploy`. **OC has no equivalent.** Highest leverage per dev-day in this entire audit.

---

## Section 5 — Visual / theming layer

(Covered in depth in the prior doc `2026-05-17-deep-parity-and-visual-spec.md` — pointing here so this audit is self-contained.)

- OC banner geometry matches Hermes pixel-for-pixel — `opencomputer/cli_banner.py` 4-region layout = `sources/hermes-agent/hermes_cli/banner.py` shape.
- OC palette = hot pink (`#FF3D8A`) → rose (`#E91E78`) → deep rose (`#C2185B`) replacing Hermes gold/amber/bronze. Already done.
- **Missing**: skin engine (`sources/hermes-agent/hermes_cli/skin_engine.py:1` ports cleanly), KawaiiSpinner, status bar, color tokens registry (29 tokens vs OC's 7).

---

## Section 6 — Things OC is ahead on (perspective check)

Don't get lost in the gaps. OC ships things none of the three sources have:

| OC capability | Code location | Hermes / Claude Code / OpenClaw equivalent |
|---|---|---|
| Layered Awareness L0-L4 + Life-Event Detector + ambient sensor | `opencomputer/awareness/` | none |
| F1 consent gate with HMAC audit chain | `opencomputer/security/consent.py` | flat approval prompt |
| F4 user-model graph + F5 decay/drift | `opencomputer/user_model/` | none |
| Plural personas + vibe classifier + companion voice | `opencomputer/agent/persona_engine.py` | flat personality list |
| Gateway-vs-CLI parity probe (10-mechanism telemetry catalog) | `opencomputer/gateway/parity_probe.py` | none |
| Profile handoff — 8-subsystem rebind on profile change | `opencomputer/profiles.py` + commit `eedaddf8` | profile-switch is restart-only everywhere else |
| Auto-skill-evolution quarantine→approve loop | `opencomputer/skills_guard/` | none |
| Sandbox scope policy + tool-loop detection | `opencomputer/sandbox/policy.py` (PR `bbd2dd68`) | none |
| Open-design daemon (managed lifecycle) | `opencomputer/open-design/` | none |
| Kanban orchestrator as native skill family | `opencomputer/kanban/` + 3 cron skills | Hermes has kanban tool only |
| 25+ hook events vs Claude Code's ~20 | `plugin_sdk/hooks.py::HookEvent` | OC ahead |
| Plugin install security scan + signed catalogs + sigstore | `opencomputer/plugins/{install_security_scan,catalog_signing,sigstore_verify,integrity}.py` | partial in Claude Code, less in Hermes |
| Plugin demand tracker (suggest enabling disabled plugin from miss) | `opencomputer/plugins/demand_tracker.py` | none |
| `delegate` tool with worktree/copy isolation + parallel batching + role escalation | `opencomputer/tools/delegate.py` | simpler in all three |

---

## Section 7 — Ranked gap list (the top 10)

Ordered by **leverage per dev-day** = (visible user impact) ÷ (effort + risk). Top 10 only; the port plan companion file has the recipes.

| Rank | Gap | Source of best impl | Effort | Leverage |
|---|---|---|---|---|
| 1 | **User markdown slash commands** (`~/.opencomputer/commands/*.md` → `/foo`) | Claude Code (`.claude/commands/`) | S (1 day) | HIGHEST — zero-code personal commands |
| 2 | **Wire the 7 unregistered slash commands** (`/background`, `/agents`, `/copy`, `/rollback`, etc.) | OC itself (they're written, just not registered) | XS (2 hours) | HIGH — they already work |
| 3 | **Wire `activation_planner.py` into the load pipeline** | OpenClaw (concept) — OC code already exists | S (1 day) | HIGH — cuts cold-start time 5-10x with 86 extensions |
| 4 | **Color tokens registry + skin engine** (29 tokens, 3 skins) | Hermes (`hermes_cli/skin_engine.py`) | M (2-3 days) | HIGH — enables every theme/UI plugin |
| 5 | **Plugin marketplaces (plural, named)** | Claude Code | M (3-4 days) | MEDIUM-HIGH — ecosystem play |
| 6 | **Hot-reload plugins** (edit + see changes, no restart) | Hermes (partial: `reload-skills`) | M (2 days) | MEDIUM-HIGH — plugin author DX |
| 7 | **KawaiiSpinner / animated tool feedback** | Hermes (`agent/display.py:573`) | M (1.5 days) | MEDIUM — visible delight |
| 8 | **`oc plugins doctor <id>`** | OC-original (extends existing pattern) | S (1 day) | MEDIUM — debugging |
| 9 | **Output styles slot** (explanatory / learning / executive plugins) | Claude Code | M (2 days) | MEDIUM — niche power-user |
| 10 | **Per-plugin update notifier** | Hermes | S (1 day) | LOW-MEDIUM — maintenance ergonomics |

**If you only ship one**: #1. User markdown commands is a 1-day change that lets every user create unlimited personal slash commands without touching Python. Highest delta-in-perceived-power per LOC in the entire audit.

**If you ship a sprint of three**: #1 + #2 + #3. ~2 days total, ships the 7 written-but-unregistered commands, adds user markdown commands, and turns on the dead activation planner. The first thing the user notices: cold-start drops from N seconds to N/5 seconds AND they suddenly have 7 new commands AND they can write their own.

**If you ship the full week (5 dev-days)**: 1 → 2 → 3 → 4 → 8. Adds tokens/skins and plugin doctor. After this OC is at "best of three on plugin architecture" and structurally ahead on awareness/security/multi-surface.

---

## Section 8 — What NOT to copy

Negative space. Some things in the three sources are net-negative for OC's users.

| Anti-pattern | Why skip |
|---|---|
| **Always-on status bar** (Hermes) | Steals one row of an 8GB M2 Air's ~30 visible rows. 3% screen-real-estate tax for cosmetic info. Privacy bleed: model name in every screenshot. **Mitigation**: ship status bar but default OFF. |
| **8 built-in skins** (Hermes) | ares/poseidon/charizard/sisyphus are vanity. Each ships a wordmark + Braille hero + faces + verbs. Test surface balloons. **Mitigation**: ship default+mono+daylight; users YAML-author the rest. |
| **31 color tokens** (Hermes) | Half never referenced in production. **Mitigation**: ship the 22 actually-used. |
| **`/yolo` mode** (Hermes) | OC's consent gate with HMAC audit chain is materially more honest than Hermes's "skip approvals" toggle. Adding `/yolo` undermines the consent invariant. **Skip.** |
| **`hermes debug share`** (Hermes) | Uploads session debug data to a Hermes-controlled server. OC's privacy posture is fail-closed local. **Skip.** |
| **`gquota` Gemini Code Assist quota** (Hermes) | Vendor-specific quota readout. OC's provider abstraction shouldn't leak per-vendor surface. **Skip.** |
| **Claude Code's flat `~/.claude/projects/` layout** | OC's per-profile `~/.opencomputer/<profile>/` rooted at `profiles.py` is materially better isolation. Don't regress. |
| **OpenClaw's `provider-replay-helpers.ts` shape** | Their replay harness is tightly coupled to TypeScript module boundaries. OC's `tests/` has cleaner async fixtures. Skip the port. |
| **Nous Portal OAuth** (`hermes login`) | Auth flow tied to Nous-hosted control plane. Out of OC's positioning. **Skip.** |
| **Plugin process isolation (sandboxed)** | Tempting in theory. None of the three implement it well; in-process is industry norm. **Don't bikeshed.** Tirith pre-exec + signed catalogs + source policy is the practical version OC already has. |

---

## Files cited (audit trail)

| Claim | File | Line |
|---|---|---|
| Activation planner exists | `opencomputer/plugins/activation_planner.py` | 47 |
| Activation planner is NOT called | grep proof: only 2 hits in tree, both self-references | — |
| OC slash registry | `opencomputer/cli_ui/slash.py` | 38 entries |
| OC slash impls (40 files, 7+ unregistered) | `opencomputer/agent/slash_commands_impl/` | — |
| Plugin loader | `opencomputer/plugins/loader.py` | — |
| Plugin discovery | `opencomputer/plugins/discovery.py` | — |
| Install security scan | `opencomputer/plugins/install_security_scan.py` | — |
| Sigstore verify | `opencomputer/plugins/sigstore_verify.py` | — |
| Catalog signing | `opencomputer/plugins/catalog_signing.py` | — |
| Source policy | `opencomputer/plugins/source_policy.py` | — |
| Installed index | `opencomputer/plugins/installed_index.py` | — |
| Demand tracker | `opencomputer/plugins/demand_tracker.py` | — |
| Subagent templates | `opencomputer/agents/{code-reviewer,explore,general-purpose,plan}.md` | 4 files |
| Hooks 25+ events | `plugin_sdk/hooks.py::HookEvent` | — |
| Consent gate | `opencomputer/security/consent.py` | — |
| Parity probe | `opencomputer/gateway/parity_probe.py` | — |
| User model | `opencomputer/user_model/` | — |
| Awareness | `opencomputer/awareness/` | — |
| Hermes plugin loader (in-process import) | `sources/hermes-agent/hermes_cli/plugins.py` | 36-46 |
| Hermes MCP subprocess spawn | `sources/hermes-agent/tools/mcp_tool.py` | 1085-1108 |
| Hermes skin engine (891 LOC) | `sources/hermes-agent/hermes_cli/skin_engine.py` | 1 |
| Hermes KawaiiSpinner | `sources/hermes-agent/agent/display.py` | 573 |
| OpenClaw plugin manifest | `sources/openclaw/src/plugins/manifest-registry.ts` | — |
| OpenClaw lazy module load (in-process import) | `sources/openclaw/src/plugins/lazy-service-module.ts` | 23-25 |
| Claude Code plugin reference | `code.claude.com/docs/en/plugins-reference` | — |
| MCP stdio transport spec | `modelcontextprotocol.io/specification/2025-06-18/basic/transports` | — |

Last verified: OC `git rev-parse HEAD` = `3849a7eb`, 2026-05-17.
