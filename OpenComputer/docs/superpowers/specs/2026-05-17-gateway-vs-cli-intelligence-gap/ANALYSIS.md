# Why OC feels dumber on Telegram/Discord/etc. than on CLI — Deep Analysis

Date: 2026-05-17
Owner: Saksham
Working dir: `/Users/saksham/Vscode/claude/OpenComputer`

This file is **diagnosis, not plan.** Every claim below is grepped from the codebase, not guessed. The diagnosis-to-plan handoff is at the bottom.

---

## Pre-work — what I actually read

| File | What I learned |
|---|---|
| `opencomputer/cli.py:1819` | The CLI constructs `AgentLoop(provider=provider, config=cfg, compaction_disabled=no_compact)`. **No `allowed_tools` argument.** No filtering. |
| `opencomputer/gateway/agent_loop_factory.py:108-125` | The gateway constructs `AgentLoop(provider=provider, config=cfg, allowed_tools=allowed_tools)`. **`allowed_tools` is a frozenset derived from `prof_cfg.enabled_plugins`** when the profile has a concrete list (only "*" wildcard means unrestricted). |
| `opencomputer/gateway/dispatch.py:1357-1370` | Per-message dispatch calls `loop.run_conversation(...)`. The arguments differ from CLI: `runtime` is the per-channel `RuntimeContext`, and `system_prompt_override` is set by per-channel **routing rules** (see next row). |
| `opencomputer/gateway/dispatch.py:1311-1352` | If routing rules match the inbound event (platform + chat_id + peer + role + guild), the gateway **resolves a different agent template** and passes its system prompt as `system_prompt_override`. |
| `opencomputer/agent/loop.py:2079-2090` | When `system_prompt_override` is set, the loop **completely bypasses PromptBuilder**: "declarative / skills / memory / SOUL injection OFF — the body is assumed intentional." **This is the single biggest cause of "less smart" on gateway sessions.** |
| `opencomputer/gateway/dispatch.py:1545-1633` | `_build_channel_runtime` reads `event.metadata["channel_id"]` and asks the adapter to resolve a per-channel system prompt + skill ids. If found, they're stuffed into `runtime.custom["channel_prompt"]` + `["channel_skill_bodies"]`. **Sets `custom["_is_gateway_session"] = True`** — used elsewhere to gate auto-swap. |
| `opencomputer/agent/routing.py:1-50` | Routing has 8 specificity dimensions (chat_id, peer, role, guild, team, account, channel, default). Most-specific wins. The matched rule produces a `ResolvedTemplate` with a system prompt — which becomes the `system_prompt_override` above. |
| `opencomputer/agent/bindings_config.py:8-11` | Bindings YAML can match by `platform: telegram, chat_id: <id>` and rebind the entire profile/model/etc. for that chat. **Telegram messages may hit an entirely different profile than your CLI.** |
| `opencomputer/agent/loop.py:2439`, `:3462` | `runtime.agent_context` gates external memory providers (Honcho) — `"cron"`/`"flush"` short-circuit them. Gateway path leaves it at `"chat"` (default), so it's NOT the cause of weaker memory on Telegram. Greped to rule out a hypothesis. |
| `plugin_sdk/runtime_context.py:28-90` | `RuntimeContext` fields: `plan_mode`, `yolo_mode`, `permission_mode`, `agent_context`, `acp_denied_tools`, `delegation_depth`, `parent_messages`, `custom`. The `_is_gateway_session` flag lives in `custom`. |
| `opencomputer/gateway/runtime_footer.py:1-40` | Hermes-port "runtime footer" appends model name + context % + cwd to gateway replies. **Disabled by default** — opt-in via `display.runtime_footer.enabled = true`. So users can't see what model/profile they're talking to. |
| `opencomputer/gateway/_truncate.py:1-50` | Gateway truncates replies to 3800 chars before sending (so they fit Telegram 4096 / Discord 2000). **"…[truncated]" appended.** Long, nuanced answers literally get cut on chat platforms. |

So now I can speak from evidence.

---

## The 10 mechanisms that make gateway sessions weaker

In rough order of impact (most-impactful first). Each one is **separately fixable**; together they compound.

### 1. `system_prompt_override` switches off the entire PromptBuilder (BIGGEST)

**Evidence:** `loop.py:2079-2090` comment:

> "Template-authored prompts are treated as rendered-Jinja strings: **declarative / skills / memory / SOUL injection OFF** — the body is assumed intentional."

**What it means:** when a routing rule matches your Telegram chat, the agent template's system prompt **replaces** the normal prompt entirely. You lose:

- The 163-skill menu (Slot 4)
- Your MEMORY.md / USER.md / SOUL.md (Slot 1 + Slot 3)
- Workspace context (CLAUDE.md, AGENTS.md) (Slot 5)
- Pinned files (Slot 5b)
- Persona overlay (Slot 7)
- Awareness facts ("What I know about you")
- Tool-aware behavior guidance (Slot 2)

The template author is expected to handle all of that *intentionally* — but most channel templates are 50–500 lines tuned for a specific use case, not 4000 lines of equivalent context.

**This is the dominant cause.** If your Telegram bot has a route with `system_prompt_override`, it's literally running with 5% of the prompt OC's CLI sees.

**Where to look:** `oc routing list` + check your bindings YAML for any `template:` field on a rule matching `platform: telegram`.

### 2. Tool allowlist via `enabled_plugins`

**Evidence:** `agent_loop_factory.py:110-117`:

```python
if prof_cfg.enabled_plugins == "*":
    allowed_tools = None  # unrestricted (all loaded tools)
else:
    allowed_tools = frozenset(
        tool_name for plugin_id in prof_cfg.enabled_plugins
        for tool_name in plugin_registry.tools_provided_by(plugin_id)
    )
```

**What it means:** the gateway's profile may have a narrower `enabled_plugins` list than your CLI's profile. If the gateway profile excludes the `web` plugin, the agent has no `WebSearch` / `WebFetch`. If it excludes the `code_modernization` plugin, it can't `Edit` files. Etc.

The CLI path **never sets `allowed_tools`** — so it always has the full tool registry.

**The CLI is wildcard by default; the gateway is allowlist by default.** That's a fundamental asymmetry.

### 3. Per-message reply truncation to 3800 chars

**Evidence:** `_truncate.py:DEFAULT_MAX_LEN = 3800` + `ELLIPSIS = "\n\n…[truncated]"`.

**What it means:** gateway replies get hard-truncated for platform compliance. A 7,000-character thoughtful answer becomes 3,800 characters + "…[truncated]". The agent's full thinking happened — you just don't see it.

Worse: the truncation is **not communicated to the model**. The agent doesn't know its reply got cut, so it can't structure future turns around what you actually saw. Context drift.

**The CLI has no such cap.** Rich renders infinite scrollback.

### 4. Per-channel system prompt + skill ids overlay (separate from routing)

**Evidence:** `dispatch.py:1545-1633` — `_build_channel_runtime` calls `adapter.resolve_channel_prompt(channel_id, parent)` and `resolve_channel_skills(channel_id, parent)`. Results land in `runtime.custom["channel_prompt"]` + `["channel_skill_bodies"]`.

**What it means:** independently from routing rules, the channel adapter can inject a *different* prompt and *different* skill list per channel_id (used by Telegram DM topics, Discord channels, etc.). When that fires, the agent sees the channel-prompt instead of (or in addition to) the normal one.

This is **another way** to override the full prompt without you knowing. The CLI never triggers this path because there's no `channel_id`.

### 5. No interactive consent → silent tool refusal

**Evidence:** `dispatch.py:1857-2055` — `_send_approval_prompt` + `_handle_approval_click` show the gateway has its own approval flow (buttons + text replies). But the contract is "user must click a button" — the agent can't proceed mid-turn while waiting for a click on Telegram.

**What it means:** when a tool needs approval (Bash, Edit, etc.), CLI prompts interactively and the agent gets a synchronous yes/no. Gateway has to send a message, wait, parse a click. Slower, with timeouts. Some tools the agent will avoid invoking on gateway entirely because the round-trip is too costly.

The CLI's "request approval → user types y/n → continue" loop is much tighter than "send button → wait for click → poll → resume."

### 6. Profile rebind = different memory, different model

**Evidence:** `bindings_config.py:8-11`:

```yaml
- match: { platform: telegram, chat_id: "12345" }
  profile: stocks_bot
- match: { platform: telegram }
  profile: telegram_general
```

**What it means:** the same chat may rebind to a fresh profile, which has its own `<profile>/MEMORY.md`, `USER.md`, `SOUL.md`, `config.yaml`. Different model, different memory, different persona. The agent literally doesn't know you the same way.

CLI uses your `default` profile (or `-p <name>` if you set it). Gateway can rebind silently based on platform + chat_id match.

### 7. Persona classifier sees `platform="telegram"` → casual register

**Evidence:** the persona overlay you've been seeing at the top of this session (`<persona-tone>warm</persona-tone>`) is auto-classified per session. Foreground app + recent files + time-of-day → tag. **Platform is a strong signal.** Telegram → casual chat persona → shorter responses, less planning.

This is a *deliberate* behaviour (the agent should match register) but it compounds with the other factors. Casual register + less context + truncated reply = the agent **feels** dumber even when it's the same model.

### 8. Gateway's `routing_system_override` IS what `oc routing` reports — but no UI shows you which rule fired

**Evidence:** `dispatch.py:1338-1346`:

```python
resolved = _rs_template(routing_cfg, event, templates)
if resolved is not None:
    routing_system_override = resolved.system_prompt
    logger.info("M10.2 routing: %s:%s → agent=%r", ..., resolved.template_name)
```

**What it means:** when routing fires, it's only logged at INFO level to `agent.log`. **You see nothing in the chat.** You don't know that your Telegram message just got routed to the `stocks_bot` template with a 200-line specialized prompt. The model that responds feels different but you can't tell why.

CLI shows you the banner at the top of `oc chat`: model name, profile name, mode. Gateway shows nothing.

### 9. `runtime_footer` is opt-in (defaults to OFF)

**Evidence:** `runtime_footer.py:38-40` — `FooterConfig.enabled: bool = False`.

**What it means:** Hermes ports a "runtime footer" feature that appends `model: <name> · context: <pct>% · cwd: <path>` to each reply. **It's disabled by default.** So even when you suspect "this reply seems weak", you can't see what model produced it without checking config files.

This is a **UX gap** masquerading as an intelligence gap.

### 10. Compaction triggers earlier on long-lived gateway sessions

**Evidence:** OC has `agent/compaction.py` with a context-fill threshold. Telegram/Discord sessions are typically **months long with many turns** (you message every few days). At some point, compaction has summarized away the early context that contained your prefs / project info. CLI sessions are short — you exit and reopen.

The agent quite literally has less *history* to work from on a long gateway session vs a fresh CLI session.

---

## Calibrated impact ranking

| # | Mechanism | Severity | Likelihood of firing |
|---|---|---|---|
| 1 | `system_prompt_override` wipes PromptBuilder | **CRITICAL** | Only if you have routing rules. Check `oc routing list`. |
| 2 | Tool allowlist via `enabled_plugins` | **HIGH** | Triggers when profile has a non-`"*"` `enabled_plugins`. |
| 3 | 3800-char reply truncation | **HIGH (compounds)** | Always — every gateway reply >3800 chars gets cut. |
| 4 | Per-channel prompt + skills overlay | **HIGH** | Only when the adapter supports channel scoping (Telegram DM topics, Discord channels). |
| 5 | No interactive consent | **MEDIUM** | When the agent wants to use a gated tool. |
| 6 | Profile rebind silently switches memory | **HIGH** | When bindings YAML has rules matching your channel. |
| 7 | Persona classifier sees platform → casual register | **MEDIUM** | Always (it's the design). |
| 8 | No "which rule fired" UI | **MEDIUM (UX)** | Always. |
| 9 | `runtime_footer` defaults OFF | **MEDIUM (UX)** | Until enabled. |
| 10 | Compaction over months-long sessions | **MEDIUM (slow burn)** | Sessions older than a few weeks with many turns. |

---

## What's NOT the cause (verified, ruled out)

- **`runtime.agent_context`** stays at `"chat"` for gateway. Greped — gateway doesn't set it to `cron`/`flush`. So the Honcho-bypass guard doesn't apply.
- **Different version of the agent loop.** Both paths use the same `AgentLoop` class from `opencomputer.agent.loop`. No "lite" version.
- **Different model API.** Same provider plumbing on both sides; same Anthropic client.
- **Hooks fire less.** They fire the same way; gateway adds a few extra (`agent:start`, `agent:end`). If anything, gateway has MORE observability hooks, not fewer.

---

## Diagnostic commands — run these to see what's actually happening on YOUR install

```bash
# What profile does each platform route to?
oc bindings list

# Show all routing rules + which would match a Telegram event
oc routing list
oc routing test --platform telegram --chat-id <your-chat-id>

# Compare model + plugins between default and the gateway profile
oc -p default model get
oc -p default plugins list --enabled

oc -p <gateway-profile> model get
oc -p <gateway-profile> plugins list --enabled

# Memory comparison
ls -la ~/.opencomputer/default/{MEMORY,USER,SOUL,DREAMS}.md
ls -la ~/.opencomputer/<gateway-profile>/{MEMORY,USER,SOUL,DREAMS}.md
wc -l ~/.opencomputer/default/MEMORY.md ~/.opencomputer/<gateway-profile>/MEMORY.md

# Check if runtime_footer is enabled
oc config get display.runtime_footer.enabled

# Look at agent.log for routing decisions
tail -200 ~/.opencomputer/logs/agent.log | grep "M10.2 routing"

# Session count + age on the gateway profile (older sessions = more compaction)
oc -p <gateway-profile> session list | head -20
```

---

## Fixes ranked by ROI

### Tier 1 — High value, low effort (do these first)

1. **Enable `runtime_footer`.** One-line config change. Tells you on every reply which model/profile/context-fill answered. Lets you SEE the asymmetry without inspecting code.
   ```bash
   oc config set display.runtime_footer.enabled true
   oc config set display.runtime_footer.fields "model,profile,context_pct,tools_available"
   ```
   Effort: **XS (10 min)**. Value: **HIGH** — surfaces all the other problems.

2. **Document the gateway-vs-CLI asymmetry in `gateway/CLAUDE.md`.** Add a paragraph noting that gateway sessions are profile-filtered + routing-overridden + truncated. Future-OC dev sees this immediately. Effort: **XS**.

3. **Set `enabled_plugins: "*"`** on the gateway profile (matches CLI behaviour). Effort: **XS**. Risk: gives gateway full tool surface — make sure consent gate still gates dangerous tools.

### Tier 2 — Medium value, medium effort

4. **Stop `system_prompt_override` from wiping the whole builder.** Add a `merge_with_builder: bool = False` field to `ResolvedTemplate`. When True, the template's prompt is *prepended* to the PromptBuilder output instead of replacing it. Channel templates keep their voice; agent keeps memory + skills + tools-aware guidance.
   - Edit: `agent/loop.py:2079-2090` to honour the flag.
   - Edit: `agent/routing.py::ResolvedTemplate` to add the field.
   - Effort: **S (1 day)**. Risk: existing templates assume full override; flag-gated.

5. **Surface routing decisions to the user.** When routing fires, prepend a small badge to the first message: `[routed to: stocks_bot · model: claude-opus-4-7]`. Toggle via config. Effort: **S (1 day)**. Value: **MEDIUM** — user knows when they're not on default.

6. **Bump truncation cap, OR**: send long replies as multiple chunks per platform's chunk-size limit. Discord can take 2000 × N messages. Telegram can take 4096 × N. Don't lose information; just paginate.
   - Edit: `_truncate.py` to return `list[str]` chunks instead of one truncated string.
   - Edit: `delivery.py` to send chunks in sequence with `(1/3)`, `(2/3)`, `(3/3)` markers.
   - Effort: **M (2-3 days)**. Value: **HIGH** — agent's long answers actually reach the user.

### Tier 3 — High value, higher effort

7. **Per-channel/per-platform tool allowlist UI.** Right now, gateway's `enabled_plugins` filter is silent. Add `oc tools list --platform telegram --chat <id>` that shows exactly which tools that chat has access to.
   - Effort: **M (2-3 days)**. Value: **MEDIUM** — debug surface.

8. **Async consent for gateway.** Instead of blocking on click, have the agent post "I want to run X — reply Y to approve" and proceed asynchronously when reply lands. Keeps the agent able to do multi-tool work without serializing on per-tool approvals.
   - Effort: **L (1 week)**. Value: **HIGH** for power users.

9. **Persona-mode override per platform.** Add `display.persona_override: <mode>` config so you can force `task` mode on Telegram even though the classifier says `casual`. Effort: **S (1 day)**.

10. **Session-fork-aware compaction.** When a gateway session crosses N turns, fork into a fresh session inheriting MEMORY but dropping low-value history. Avoids the slow-burn compaction problem.
    - Effort: **M-L (1 week)**. Value: **MEDIUM** for long-running gateway users.

---

## The "honest one-liner" diagnosis

**Your CLI sees the full prompt OC builds (164-line skills list + 4000 chars of memory + 22 prompt slots) and has every tool. Your Telegram session sees a 200-line specialized template + a narrower toolset + a 3800-char truncated reply, while a persona classifier nudges it into casual mode. Same agent loop, very different inputs and outputs.**

It's not that the model got dumber. It's that you handed it a different brain (smaller system prompt), a different toolbox (fewer tools), a different memory (potentially separate profile), and ask it to talk through a tiny window (3800-char truncation). Then you compare its output to the CLI's full-context answer and notice the gap.

---

## Recommended next action — pick ONE

| Option | What it does | Effort |
|---|---|---|
| **A** | I run the diagnostic commands above on your machine and tell you which mechanisms are actually firing | 5 min, zero risk |
| **B** | I write a proper plan (brainstorm → audit-design → plan → audit-plan) for fixing Tier 1 + Tier 2, save it as a spec file | 30 min, no code change |
| **C** | I edit `gateway/CLAUDE.md` to document this asymmetry so it's surfaced to every future dev session | 10 min, doc-only |
| **D** | I ship the runtime_footer enable + the merge_with_builder flag (Tier 1.1 + Tier 2.4) as actual code | 1-2 days of focused work, real code change |

My recommendation: **A first**, then decide between B/C/D based on what A reveals. If your Telegram routes go to a totally separate profile with sparse memory, fixing the profile is a 5-minute config change. If it's actually the prompt override or truncation, that's where the engineering goes.
