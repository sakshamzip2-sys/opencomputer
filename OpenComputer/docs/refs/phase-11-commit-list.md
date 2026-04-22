# Phase 11 — Consolidated commit list

Synthesis of `docs/refs/{claude-code,hermes-agent,kimi-cli,openclaw}/inventory.md` against current OpenComputer surface. **This file is the work plan for sub-phases 11b → 11e.** Ordered by sub-phase; within each sub-phase, ordered by impact-per-day-of-effort.

Legend: **S** = <1 day · **M** = 1–3 days · **L** = >3 days. Origin column tags which inventory the row came from. "Already" means the prior phase already covered it (referenced for traceability only).

---

## Phase 11b — Claude Code tool parity (1 week)

Hard scope (per Ultraplan-approved spec). Coding-harness rows ship only after the parallel session signals done; until then, that part of 11b stages on a follow-up branch.

| # | Item | Origin | Destination | Effort | Notes |
|---|---|---|---|---|---|
| 11b.1 | `NotebookEdit` tool | claude-code | `opencomputer/tools/notebook_edit.py` | M | Read already handles `.ipynb`; we only need write/insert/delete cells. |
| 11b.2 | `AskUserQuestion` tool | claude-code + hermes (clarify_tool) | `opencomputer/tools/ask_user_question.py` | M | Routes via active channel adapter. New `plugin_sdk/interaction.py` exports `InteractionRequest` + `InteractionResponse`. Pending-reply state via `SessionDB`. |
| 11b.3 | `PushNotification` tool | claude-code + kimi (notifications subsystem) | `opencomputer/tools/push_notification.py` | S | New optional `BaseChannelAdapter.send_notification()` with default = same as `send()`. Telegram/Discord override. |
| 11b.4 | `Skill` (invocable) tool | claude-code | `opencomputer/tools/skill.py` | S | Thin wrapper over `MemoryManager.list_skills` + `load_skill_body` to *read* a skill; existing `SkillManage` stays for *write*. |
| 11b.5 | Hook-event expansion (PreCompact, SubagentStop, Notification, UserPromptSubmit count to 4 missing) | claude-code | `plugin_sdk/hooks.py` + `opencomputer/hooks/engine.py` | S | Adds 3 enum values + dispatch sites. UserPromptSubmit already implemented. |
| 11b.6 | `ExitPlanMode` tool | claude-code (kimi has it too) | `extensions/coding-harness/tools/exit_plan_mode.py` | S | Coding-harness only — wait for parallel session signal. |
| 11b.7 | `Monitor` tool | claude-code | `extensions/coding-harness/tools/monitor.py` | S | Wraps existing `background.py`. Coding-harness only. |
| 11b.8 | `BashOutput` / `KillShell` tools | claude-code | `extensions/coding-harness/tools/background_io.py` | S | Splits the existing Background tool's read/kill into discrete model-facing tools. Coding-harness only. |

**Total core (11b.1-5):** 1 PR, ~M+M+S+S+S = 4-5 days. **Coding-harness add-on (11b.6-8):** separate PR after the parallel session signals done, ~S+S+S = 1-2 days.

**plugin_sdk surface delta:** +`interaction.py` (2 new types), +`send_notification` default method, +3 hook events. SDK boundary test must remain green.

---

## Phase 11c — MCP expansion (1 week)

| # | Item | Origin | Destination | Effort | Notes |
|---|---|---|---|---|---|
| 11c.1 | HTTP/SSE transport | claude-code (.mcp.json supports it) | `opencomputer/mcp/client.py` (fill `NotImplementedError` at line 119) | M | Use `mcp.client.sse.sse_client` (transitive dep). Config switch in `MCPServerConfig`. |
| 11c.2 | `MCPResource` type + injection bridge | claude-code | `opencomputer/mcp/client.py` + new `MCPResourceInjectionProvider` (reuses existing ABC) | M | Surfaces resources as `@mention` injectable via the existing `DynamicInjectionProvider`. |
| 11c.3 | `MCPPrompt` type + slash-command bridge | claude-code | `opencomputer/mcp/client.py` + slash-command dispatcher | M | Registers `/<server>:<prompt>` reusing the slash plumbing kimi has us building (11d.x). |
| 11c.4 | `opencomputer mcp` CLI subcommand | hermes + kimi (both have it) | `opencomputer/cli.py` | M | `add / list / remove / test / enable / disable`. Writes via `config_store.set_value`. |
| 11c.5 | `docs/mcp-catalog.md` | claude-code | `docs/mcp-catalog.md` (new) | S | Curated list (filesystem, git, github, sequential-thinking, fetch, memory) with `opencomputer mcp add <preset>` snippets. Not bundled. |
| 11c.6 | MCP OAuth 2.1 client | hermes (mcp_oauth) | `opencomputer/mcp/oauth.py` (new) | M | Token persistence + localhost callback server pattern. Required for github/notion-class MCPs. |

**Total:** 1 PR, ~6 days.

---

## Phase 11d — Best-of extractions (3-5 days)

Per the Ultraplan-approved spec: **episodic memory + batch runner are core 11d**. The rows below are the "best-of" rows from inventories that fit naturally into 11d. The extras are flagged "11d-stretch" — pick by appetite.

| # | Item | Origin | Destination | Effort | Notes |
|---|---|---|---|---|---|
| 11d.1 | Episodic memory (third pillar) | hermes + openclaw (memory-core) | `opencomputer/agent/memory.py` + `opencomputer/agent/state.py` (new table + FTS5) | M | Per-turn summary log → SessionDB. Top-k retrieval via existing FTS5 infra (no new dep). |
| 11d.2 | Batch runner | claude-code (Task pattern) + anthropic SDK | `opencomputer/batch.py` (new) + `opencomputer cli batch <file>` subcommand | M | JSONL of prompts → `messages.batches.create`. |
| 11d.3 | `model_metadata` (context windows + pricing) | hermes | `opencomputer/agent/model_metadata.py` (new) | S | Centralised lookup. CompactionEngine migrates to use it. Sets up usage_pricing later. |
| 11d.4 | `web-fetch-visibility` (SSRF + redirect-loop guards) | openclaw | `opencomputer/tools/web_fetch.py` (extend) | S | Reject internal IPs, cap redirects, log denies. |
| 11d.5 | Multi-provider WebSearch dispatch | openclaw | `opencomputer/tools/web_search.py` (extend) | S | Provider chain: DDG (default) → Brave (api-key flag) → Tavily (api-key flag). |
| 11d.6 | `sessions-list-tool` / `sessions-history-tool` / `session-status-tool` | openclaw | `opencomputer/tools/session_ops.py` (new) | S | Tool wrappers around existing SessionDB + sessions CLI. |
| 11d.7 (stretch) | Slash-command routing generalisation | kimi | `opencomputer/agent/slash_router.py` (new) + integration in cli + coding-harness | M | Lets any plugin register `/<plugin>:<cmd>`. Pairs with 11c.3. |
| 11d.8 (stretch) | Auxiliary client + credential pool | hermes | `opencomputer/agent/credential_pool.py` (new) | M | Multi-provider router with fallback on credit exhaustion. Big upgrade, but big surface. |

**Total core (11d.1-6):** 1 PR, ~M+M+S+S+S+S = 5-6 days. **Stretch (11d.7-8):** 2-4 extra days; only if 11d.1-6 lands faster than expected or appetite for it surfaces during dogfood.

---

## Phase 11e — Post-gate (only ship what dogfood demanded)

Ordered by current expected demand (revisit after the 2-week dogfood gate).

| # | Item | Origin | Destination | Effort | Trigger |
|---|---|---|---|---|---|
| 11e.1 | Slack channel | hermes + openclaw | `extensions/slack/` | M | Already on the post-gate list. Keep. |
| 11e.2 | Local-inference providers (ollama, lmstudio) | openclaw | `extensions/ollama-provider/` + `extensions/lmstudio-provider/` | S each | If user runs anything on a local box. |
| 11e.3 | Cron / scheduler | hermes | `opencomputer/cron/` (new package) + CLI | M | If user wants scheduled agent runs. |
| 11e.4 | ACP/IDE channel | kimi (ACP) + hermes (acp_adapter) | `extensions/acp/` | L | If user wants editor integration. |
| 11e.5 | Bedrock / Gemini providers | hermes (bedrock_adapter) | `extensions/bedrock-provider/` + `extensions/gemini-provider/` | M each | Only if hit. |
| 11e.6 | API server (OpenAI-compat HTTP) | hermes (api_server) + kimi (Web UI + API) | `extensions/api-server/` | L | If user wants OpenComputer to back another OpenAI-SDK client. |
| 11e.7 | Email channel | hermes | `extensions/email/` | M | If demand surfaces. |
| 11e.8 | Webhook channel (generic) | hermes | `extensions/webhook/` | M | If demand surfaces. |
| 11e.9 | Matrix channel | hermes + openclaw | `extensions/matrix/` | M | If demand surfaces. |
| 11e.10 | Memory plugins (lancedb, wiki) | openclaw | `extensions/memory-vector/`, `extensions/memory-wiki/` | M each | After 11d.1 episodic memory ABC lands AND user wants vector/wiki backend. |
| 11e.11 | Browser tool (Playwright) | hermes (browser_tool) + openclaw (browser plugin) | `extensions/browser/` | L | If web-tool gaps surface during dogfood. |
| 11e.12 | Image-gen + vision plugins | hermes + openclaw | `extensions/image-gen/`, `extensions/vision/` | M each | If multi-modal use cases surface. |
| 11e.13 | Asia-region channels (weixin, feishu, dingtalk, zalo, bluebubbles) | hermes + openclaw | `extensions/<channel>/` | M each | Only if user explicitly asks. |
| 11e.14 | dev-tools plugin (claude-code's plugin-dev set) | claude-code | `extensions/dev-tools/` | L | If plugin-author ecosystem activity warrants. |

**11e has no fixed scope or schedule** — closes when there are no items left with a fired trigger.

---

## Cross-cutting reminders

These come from the inventories AND the Ultraplan-approved Phase 11 spec. Read before any sub-phase PR.

1. **Plugin-SDK boundary**: every new tool / channel method respects the rule that `plugin_sdk/*` does not import from `opencomputer/*`. The `tests/test_phase6a.py` boundary test stays green.
2. **Coding-harness ownership**: 11b.6/7/8 wait for the parallel session's signal; until then, ship only the core 11b rows.
3. **Don't default-install anything**: MCPs and provider plugins ship through the catalog (`docs/mcp-catalog.md`) or PyPI separately. No new bundled plugins shipped on default install.
4. **One PR per sub-phase**: 11a (this) → 11b-core PR → 11b-coding-harness PR → 11c PR → 11d PR. Each must be green-CI before the next starts.
5. **Dogfood gate (CLAUDE.md §5)**: 11e items only land after 2 weeks of real use produces concrete demand for each.

---

## Status

- 11a inventory: **DONE** (this commit).
- 11b: not yet started. Branch `phase-11b/claude-code-parity` (cut from main after PR #1 merges).
- 11c, 11d, 11e: not yet started.
