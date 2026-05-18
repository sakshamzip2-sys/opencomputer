# Gateway vs CLI — Extended Gap Analysis (2026-05-18, revised)

Date: 2026-05-18 (revised after audit pass)
Owner: Saksham
Working dir: `/Users/saksham/Vscode/claude/OpenComputer`
Supersedes-extends: `docs/superpowers/specs/2026-05-17-gateway-vs-cli-intelligence-gap/ANALYSIS.md` (the original 10-mechanism diagnosis) and `docs/superpowers/specs/2026-05-17-gateway-vs-cli-parity/PLAN.md` (the M1-M4 plan).

---

## Honesty note (this revision)

The first version of this doc was written from memory + ~5 targeted greps. After the user asked "are you sure", I audited the full `opencomputer/gateway/` tree (32 modules, ~12,000 LOC), the slash-command system (`opencomputer/agent/slash_commands_impl/`, ~35 commands), `plugin_sdk/channel_contract.py`, and the queue manager. This revision **corrects four wrong claims**, **fills sixteen gaps the first pass missed**, and **lowers some priorities** because the infrastructure already exists — only the wiring is missing.

Corrections from v1:
- **A1 (streaming).** v1 said "no streaming infrastructure." Wrong. `plugin_sdk/streaming/BlockChunker`, `wrap_stream_callback`, and `opencomputer/gateway/streaming_chunker.py` exist. The gap is that `dispatch.py` never passes a `stream_callback` to `loop.run_conversation`. Fix is wiring, not building.
- **A4 (ESC/cancel).** v1 said "no cancel infrastructure." Wrong. `opencomputer/gateway/queue_manager.py` has `interrupt` / `steer` / `collect` / `followup` modes. `/queue-mode` slash command exists. Gap: it's class-based slash and dispatcher doesn't run it (A3 gap). Fix is A3 + per-chat default.
- **B3 (capability matrix).** v1 said "no `AdapterCapabilities` struct." Wrong. `plugin_sdk/channel_contract.py:95` defines `ChannelCapabilities` flag enum. `oc adapter capabilities` CLI exists. Gap: the agent's prompt never reads it.
- **A3 (slash commands).** v1 described slash commands as `@app.command` decorated CLI fns. Wrong. They are `SlashCommand` subclasses with a unified dispatcher (`opencomputer/agent/slash_dispatcher.py`). The architecture is already unified; gateway just doesn't dispatch the non-`bypass_running_guard=True` ones.

Net effect: many gaps are smaller engineering jobs than v1 suggested. The map is unchanged; the costs are lower.

---

## Why this doc exists

The original parity work catalogued **10 mechanisms** that make a gateway turn behave differently from a CLI turn. M3 shipped fixes for all of them. But "ten mechanisms fixed" is not "gateway = CLI." The catalogue scoped itself to the dispatcher's prompt-building and outgoing path. There are more gaps — structural, affordance-based, and architectural — that the catalogue did not include.

This doc enumerates **every gap** found by reading the gateway tree end-to-end. Each gap is grepped to `file:line` so a fresh session can verify before fixing.

---

## How to use this doc

Sections are **independent**. Each gap has:

- **What** — gap in one sentence
- **Evidence** — file:line citation
- **Why it makes gateway feel dumber** — user-visible effect
- **Fix** — code-level change
- **Effort** — XS (hours), S (1-3 days), M (3-7 days), L (1-3 weeks), XL (1+ month)
- **Priority** — P0 (next), P1 (this quarter), P2 (later), P3 (niche)
- **Dependencies**

Execute in priority then effort order.

---

## Section A — Affordance gaps (CLI levers that don't exist on gateway)

### A1. Streaming infrastructure exists but is unwired

- **What.** CLI passes `stream_callback=renderer.on_chunk` to the loop (`opencomputer/cli.py:2346`, `2422`). Gateway calls `loop.run_conversation(...)` (`opencomputer/gateway/dispatch.py:1579-1595`) with no `stream_callback`. The streaming chunker (`opencomputer/gateway/streaming_chunker.py`) and `plugin_sdk/streaming/BlockChunker` are built and tested (`tests/streaming/test_block_chunker.py`). Wire server uses them (`opencomputer/gateway/wire_server.py:1039`). The *gateway daemon* does not.
- **Evidence.** `grep -n "stream_callback\|BlockChunker" opencomputer/gateway/dispatch.py` → nothing. `streaming_chunker.py:1-30` documents the async chunker with humanlike pacing. `wire_server.py:1039` uses it.
- **Why it makes gateway feel dumber.** Long generations appear as "nothing → wall of text." On CLI you watch tokens arrive. On Telegram you wait, then receive.
- **Fix.** In `dispatch.py:_do_dispatch_inner`, when adapter has `ChannelCapabilities.EDIT_MESSAGE`:
  1. Send a placeholder message, capture `message_id`.
  2. Build a `BlockChunker` with `on_block = lambda text: adapter.edit_message(chat_id, message_id, text)`.
  3. Pass `wrap_stream_callback(chunker)` to `run_conversation`.
  4. On turn-end, replace the streaming placeholder with the final body (chunked if over cap).
  
  For Tier 3/4 adapters (no edit-message), one-shot delivery stays as today.
- **Effort.** M. ~1 week. Infra exists; need adapter-id dispatch + state machine for placeholder → final.
- **Priority.** **P0.** Single biggest perceived-intelligence improvement.
- **Dependencies.** B3 already done — `ChannelCapabilities` exists.

### A2. No plan mode on gateway

- **What.** `RuntimeContext.plan_mode: bool` exists (`plugin_sdk/runtime_context.py:28`). CLI sets it via `_cmd_chat(plan=plan)`. Gateway's `_build_channel_runtime` (`dispatch.py:1949-1969`) never populates `plan_mode`. The wire protocol schema declares it (`opencomputer/gateway/protocol_v2.py:136`) but the gateway dispatcher does not consume it.
- **Evidence.** `grep -n "plan_mode" opencomputer/gateway/dispatch.py` → nothing.
- **Why it makes gateway feel dumber.** No "tell me before you do" affordance on Telegram. Every message is full-execute.
- **Fix.** `/plan on|off` slash command (a class in `opencomputer/agent/slash_commands_impl/plan_cmd.py`). Persist toggle in `<profile>/gateway/runtime_state.json` keyed by `(platform, chat_id)`. Inject in `_build_channel_runtime`.
- **Effort.** S. ~2 days.
- **Priority.** **P1.** Hard "you cannot do this on gateway" today.
- **Dependencies.** A3 (slash commands need dispatch).

### A3. Slash commands silently no-op on gateway (only `bypass_running_guard=True` ones run)

- **What.** Slash commands are unified: `SlashCommand` subclasses in `opencomputer/agent/slash_commands_impl/` (35+ classes), dispatched via `opencomputer/agent/slash_dispatcher.py`. Each command can declare `bypass_running_guard=True` to skip the chat lock. **Only `KanbanCommand` declares it today** (`opencomputer/kanban/slash_command.py:191`). Every other slash command on gateway is treated as text in `dispatch.py:2024` (`if not getattr(cmd, "bypass_running_guard", False): return None`) and the slash text reaches the model.
- **Evidence.** `grep -rn "bypass_running_guard\s*=\s*True" opencomputer/` returns 1 hit: kanban. The class list at `opencomputer/agent/slash_commands_impl/` is: `agents`, `auto`, `background`, `batch`, `bell`, `branch`, `btw`, `busy`, `capabilities`, `checkpoint`, `context`, `copy`, `details`, `display_toggles` (`/verbose`, `/statusbar`), `fast`, `footer`, `handoff`, `history`, `indicator`, `mode`, `mouse`, `persona_mode`, `platforms`, `plugin_reload`, `policy` (5 subclasses), `profile_suggest`, `queue_mode`, `reasoning`, `restore`, `rollback`, `save`, `scrape`, `sethome`, `skin_personality`, `sources`, `status`, `title`, `undo`, `update`, `usage`, `yolo`, `auto` — none gateway-safe today.
- **Why it makes gateway feel dumber.** No `/clear`, no `/branch`, no `/save`, no `/handoff`, no `/sources`, no `/history`, no `/checkpoint`, no `/usage`, no `/auto`, no `/yolo`, no `/queue-mode`, no `/persona`, no `/reasoning`. Every CLI lever silently doesn't exist on Telegram.
- **Fix.** Audit each `SlashCommand` subclass; declare one of:
  1. **`gateway_safe=True`** — execute inline, return text, do not invoke agent loop. Examples: `/status`, `/context`, `/sources`, `/history`, `/usage`, `/skin list`, `/persona list`, `/skill list`, `/agents`, `/platforms`, `/capabilities`.
  2. **`gateway_safe=True, bypass_running_guard=True`** — state-mutating but quick, runs even mid-turn. Examples: `/clear`, `/branch`, `/save <name>`, `/restore <id>`, `/queue-mode`, `/auto`, `/yolo`, `/title`, `/sethome`.
  3. **CLI-only — explicit refusal.** `/checkpoint` (assumes a worktree), `/edit` (TUI), `/tui`. Return "not supported on gateway."
  
  In `dispatch.py:2024`, change `if not getattr(cmd, "bypass_running_guard", False)` to also accept `gateway_safe=True`.
- **Effort.** M. ~1 week. Audit + tag 35 commands + dispatcher branch + tests.
- **Priority.** **P0.** Every other affordance gap (A2, A4, A8, C1, C4, D1, D2, D3, E3) reuses this.
- **Dependencies.** None.

### A4. ESC / `/cancel` mid-turn — infrastructure exists, default mode is wrong

- **What.** `opencomputer/gateway/queue_manager.py` already supports `interrupt` / `steer` / `collect` / `followup` modes. Default is `followup` (legacy serialize-and-wait). `/queue-mode interrupt` would let a new message cancel the in-flight turn.
- **Evidence.** `queue_manager.py:7-13` lists the four modes. `slash_commands_impl/queue_mode_cmd.py` is the class. But it's not `bypass_running_guard=True`, so on gateway it falls through to the model.
- **Why it makes gateway feel dumber.** You cannot stop the agent on Telegram. It runs to completion no matter what you type next.
- **Fix.** Two parts:
  1. Tag `QueueModeCommand` as `gateway_safe=True, bypass_running_guard=True` (covered by A3).
  2. Per-chat default mode in bindings:
     ```yaml
     bindings:
       - match: { platform: telegram }
         queue_mode: interrupt   # new message cancels in-flight
     ```
  3. Surface in a chat: respond to `/cancel` and `/stop` as aliases for "interrupt the current turn now without queueing a new message."
- **Effort.** S. ~3 days. Infrastructure works; needs slash tagging + bindings field + alias.
- **Priority.** **P1.** Bites multi-tool turns; OK to defer behind A3.
- **Dependencies.** A3.

### A5. Async consent — multi-tool turns serialize on each approval

- **What.** When a gateway turn hits a gated tool, the per-chat lock holds while a button approval round-trips. A turn needing N gated tools pays N round-trips, each 5-30 seconds.
- **Evidence.** `dispatch.py:1857-2055` — `_send_approval_prompt` + `_handle_approval_click`. `dispatch.py:424` — per-chat lock. `docs/gateway/deferred-parity-work.md` labels this "optional enhancement, not a parity gap" — that's wrong for technical workflows.
- **Why it makes gateway feel dumber.** Agent routes around gated tools rather than ask, because each ask is expensive. Fewer Bash / Edit / Write calls per turn = "doesn't actually do anything."
- **Fix.** Real async consent state machine:
  1. Post approval message with `consent_id`.
  2. Release per-chat lock.
  3. Snapshot turn state to `<profile>/gateway/pending_consents/{consent_id}.json` (serialized `RuntimeContext`, partial conversation, tool plan).
  4. On user reply (button or text), look up snapshot and resume the turn.
  5. Idempotency: consent_id is the dedupe key; double-clicks no-op.
- **Effort.** L. 1-2 weeks. Real engineering — turn-state serialization in `AgentLoop` does not exist today.
- **Priority.** **P1.** Was P3 ("latency enhancement") in deferred-parity-work.md — promoting because it's the dominant felt gap for tool-heavy workflows.
- **Dependencies.** Requires `AgentLoop.snapshot()` / `AgentLoop.resume_from_snapshot()` to be built.

### A6. No `cwd` — file tools operate in the daemon's process directory

- **What.** `dispatch.py:1736` passes `cwd=os.getcwd()` to the runtime footer. That's the daemon's launch directory, not the user's project root. Tool dispatch never overrides this. There is no per-chat `cwd` config in `bindings.yaml`.
- **Evidence.** `grep -n "cwd" opencomputer/gateway/dispatch.py` — one footer use; nothing on tool side. `extensions/coding-harness/tools/read.py` resolves paths from `os.getcwd()`.
- **Why it makes gateway feel dumber.** "Read PLAN.md" reads `~/.opencomputer/<profile>/PLAN.md` (or wherever the daemon was launched), not your project's PLAN.md. Bash runs in the wrong directory. Every file-touching tool is blind.
- **Fix.** Per-chat `cwd` in bindings:
  ```yaml
  bindings:
    - match: { platform: telegram, chat_id: "12345" }
      cwd: /Users/saksham/Vscode/claude/OpenComputer
  ```
  Inject to `RuntimeContext.custom["cwd"]`; plumb into tool dispatch path. Optional slash command `/cwd <path>` for inline change.
- **Effort.** S. ~3 days.
- **Priority.** **P0.** Without this, gateway file tools are useless for the user's actual project.
- **Dependencies.** None.

### A7. No gateway session banner

- **What.** CLI `_render_chat_banner` (`cli.py:1519`) prints model / profile / plugins / MCP / cwd at session start. Gateway sends nothing on first inbound.
- **Evidence.** `dispatch.py` has no banner call. M3's `↪ routed:` badge only fires on routing-rule match and only once per session.
- **Why it makes gateway feel dumber.** No way to know which profile / model / cwd answered the first message.
- **Fix.** On session-first-inbound, send a one-line greeting:  
  `OpenComputer · profile=<name> · model=<id> · cwd=<path> · skills=<count>`  
  Suppress with `display.gateway_banner.enabled = false` for bot deployments that need silence.
- **Effort.** XS. ~half a day.
- **Priority.** **P1.** Makes the asymmetry legible.
- **Dependencies.** None.

### A8. No mid-session profile swap via slash

- **What.** `ProfileRebindRegistry` exists (`opencomputer/agent/profile_rebind.py`) and is exercised by Ctrl+P / `/handoff` on CLI. Gateway has bindings-based profile resolution at session bootstrap, no mid-session swap path.
- **Evidence.** `grep -n "handoff\|swap_profile" opencomputer/gateway/dispatch.py` returns one comment about *gating* auto-swap, no trigger. `HandoffCommand` class exists in `slash_commands_impl/handoff_cmd.py` but is not `gateway_safe=True`.
- **Why it makes gateway feel dumber.** Stuck with the profile bindings chose at session start.
- **Fix.** Tag `HandoffCommand` `gateway_safe=True, bypass_running_guard=True`. The rebind plumbing already handles all the cross-cutting state.
- **Effort.** XS. ~half a day after A3 ships.
- **Priority.** **P2.**
- **Dependencies.** A3.

### A9. No `/queue-mode collect` discovery — message-burst handling is invisible

- **What.** `collect` mode (`queue_manager.py:8-11`) debounces inbound messages and drains them as one agent run. Perfect for users who type three short messages in a row on Telegram. Default is `followup` — three messages = three agent turns.
- **Evidence.** Default in `queue_manager.py:73` is `DEFAULT_COLLECT_DEBOUNCE_S`, but `default_mode` is `"followup"` unless set in config.
- **Why it makes gateway feel dumber.** "Wait — I meant" / "actually, also do X" creates three turns where one would do. The agent half-answers each.
- **Fix.** Default `collect` mode for chat-style platforms (telegram, discord, whatsapp). `followup` for batch-style (email, webhook, sms). Configurable in bindings; surface via `/queue-mode`.
- **Effort.** XS. ~half a day. Config default flip + tier-based.
- **Priority.** **P1.** Very high felt impact; very small change.
- **Dependencies.** None.

---

## Section B — Output / display gaps

### B1. `max_tokens` cap clips at generation time

- **What.** Reply chunker splits over-cap *sent* bodies (`reply_chunker.py`). But `max_tokens` (provider call cap, `loop.py:5517`) cuts at generation time. Pure-text replies that hit `stop_reason=max_tokens` don't auto-continue (only tool-use replies do, `loop.py:3108-3133`).
- **Evidence.** Read source.
- **Why it makes gateway feel dumber.** Long code answers clipped before the chunker even sees them. The chunker delivers `(3/3)` cleanly, but the content stops mid-thought because the model ran out of tokens.
- **Fix.** Detect `stop_reason="max_tokens"` on pure-text → emit a continuation call ("continue from where you left off; do not repeat") and concatenate.
- **Effort.** S. ~2 days.
- **Priority.** **P1.**
- **Dependencies.** None.

### B2. Reasoning blocks hidden everywhere by default

- **What.** `display_config.py:28, 48, 54, 60, 66` — `show_reasoning: False` on every tier including Tier 1.
- **Evidence.** Source.
- **Why it matters.** Hidden reasoning makes answers look unsupported. User cannot see the model "talk itself through" hard problems.
- **Fix.** Default `show_reasoning: True` on Tier 1 (telegram, discord). Render as italic preamble. Add `/reasoning on|off` (class already exists at `slash_commands_impl/reasoning_cmd.py`) — tag `gateway_safe=True`.
- **Effort.** S. ~2 days.
- **Priority.** **P2.**
- **Dependencies.** A3.

### B3. Capability matrix exists but the model doesn't see it

- **What.** `ChannelCapabilities` flag enum exists (`plugin_sdk/channel_contract.py:95`) with `TYPING`, `REACTIONS`, `VOICE_OUT/IN`, `PHOTO_OUT/IN`, `DOCUMENT_OUT/IN`, `EDIT_MESSAGE`, `DELETE_MESSAGE`, `THREADS`. Each adapter declares its flags. `oc adapter capabilities` CLI lists them. **The agent's system prompt does not include the active adapter's capabilities** — the model produces format-agnostic output.
- **Evidence.** `cli_adapter.py:159-195`. `BaseChannelAdapter.capabilities` (`channel_contract.py:155`). `grep -rn "capabilities" opencomputer/agent/prompt_builder.py` → no use.
- **Why it makes gateway feel dumber.** Agent emits Markdown on SMS (no support → renders as raw), or doesn't emit images on platforms that support them, or doesn't react with emojis when reactions are available.
- **Fix.** Add a `<adapter-capabilities>` slot in the prompt builder. On gateway dispatch, fill it from the active adapter's `capabilities` + `max_message_length`. Slot tells the model "this platform supports: edit_message, photo_out, reactions, max 4096 chars" — agent adapts output format.
- **Effort.** S. ~2 days.
- **Priority.** **P1.** Cheap; instantly improves output fit per platform.
- **Dependencies.** None.

### B4. Markdown rendering is per-adapter, not unified

- **What.** Telegram has its own escape rules (Markdown V2). Discord, Slack, WhatsApp, IRC, SMS all differ. Each adapter does its own escape or strip on send. No central "render assistant message for platform X" layer.
- **Evidence.** `extensions/telegram/adapter.py:979` chunks for Telegram; uses `_chunk_for_telegram`. Others vary.
- **Why it makes gateway feel dumber.** Same model output looks like different quality on different adapters.
- **Fix.** `gateway/render.py` with per-adapter render rules. Tied to B3's capability matrix (Markdown / code-block support flags).
- **Effort.** M. ~1 week.
- **Priority.** **P2.**
- **Dependencies.** B3.

### B5. No "compact tool-progress" mode

- **What.** `display_config.py:27` — `tool_progress: "all" | "new" | "off" | "verbose"`. No `"compact"` (one-liner per tool).
- **Why it matters.** `all` is noisy on chat; `off` hides progress. The Goldilocks middle doesn't exist.
- **Fix.** Add `"compact"` mode emitting `▸ Bash · 0.4s · exit 0` per call. Default on Tier 1.
- **Effort.** S. ~2 days.
- **Priority.** **P2.**

### B6. Outbound files (e.g. `Write` tool output, generated images) not auto-dispatched as adapter files

- **What.** When the agent generates an image (`ImageGenerate`) or writes a file (`Write`), the result is a URL or a path. The text reply mentions it. There's no auto-attachment.
- **Evidence.** `BaseChannelAdapter.send_document`, `send_image`, `send_voice` exist but the tool-result post-processor doesn't route to them.
- **Fix.** Tool-result interceptor: detect "produced file at X" or "image URL Y" patterns, route through `adapter.send_image` / `send_document` when capability supports.
- **Effort.** M. ~1 week.
- **Priority.** **P2.**
- **Dependencies.** B3.

---

## Section C — Memory / context gaps

### C1. Compaction summarises the middle of long sessions

- **What.** `preserve_anchor=True` keeps the first message verbatim. The middle gets compacted as the session grows. Gateway sessions are months-long; CLI sessions are hours-long.
- **Evidence.** `opencomputer/agent/compaction.py`.
- **Fix.** Three independent options:
  1. **Pinned messages.** `/pin <message_id>` keeps a specific past turn verbatim through compaction.
  2. **Compaction-triggered MEMORY writes.** Before each compaction, extract durable facts to MEMORY.md so they survive bland summaries.
  3. **Auto-fork at N turns.** `/auto-fork on` triggers a session fork at threshold, inheriting MEMORY but starting fresh.
- **Effort.** 1: S (~3 days). 2: M (~1 week). 3: M (~1 week).
- **Priority.** **P1** (option 1). **P2** (others).
- **Dependencies.** A3 for `/pin`.

### C2. Filesystem is daemon-local

- **What.** `Read`/`Edit`/`Write`/`Grep`/`Glob` operate on the daemon's filesystem. If the daemon runs on a server, the user's laptop files are unreachable.
- **Evidence.** All file tools resolve paths against `os.getcwd()` or absolute paths on the daemon FS.
- **Fix.** A6 (per-chat cwd) covers the "same machine" case. The "different machine" case requires a remote-FS proxy — out of scope for this doc.
- **Priority.** A6 = **P0**. Remote proxy = **P3** (separate spec).

### C3. Persona register skews casual on every gateway turn

- **What.** Persona classifier (`opencomputer/awareness/personas/classifier.py`) reads platform + recent context. Telegram → `warm` register. Even with M3's `display.persona_override`, the underlying classifier still tags the session — the override pins a persona name but the *register* (warm/task) is a separate dimension that follows agent_context.
- **Evidence.** `dispatch.py:1369-1401` — `agent_ctx = "chat"` for every gateway turn.
- **Fix.** Per-chat `register: warm | task | reflective` in bindings. Inject directly into the persona slot, bypass the classifier.
- **Effort.** S. ~2 days.
- **Priority.** **P1.**
- **Dependencies.** None.

### C4. No `/resume <id>` to attach CLI history to a gateway chat

- **What.** Session id is deterministic `sha256(platform + chat_id)`. No way to load a different session's history.
- **Fix.** `/resume <session-id-prefix>` rebinds current chat to that session id.
- **Effort.** S. ~2 days.
- **Priority.** **P2.**
- **Dependencies.** A3.

### C5. Cross-session mirroring exists but is invisible from the user's side

- **What.** `gateway/mirror.py` writes mirror entries when one session sends to another (cron, `messages_send`, `DeliveryRouter`). The agent receiving the mirror sees it in transcript. But the user has no way to query "what got sent on my behalf to this chat?"
- **Evidence.** `mirror.py:1-15`.
- **Fix.** `/mirrors` slash command lists recent mirror entries for the current chat with their source session.
- **Effort.** XS. ~half a day.
- **Priority.** **P3.**
- **Dependencies.** A3.

### C6. BOOT.md startup instructions don't render to the user

- **What.** `BOOT.md` (gateway/boot_md.py) runs a one-shot agent at gateway startup. Output goes to `[SILENT]` by default. The user has no way to see what BOOT.md did this morning.
- **Evidence.** `boot_md.py:5-15`.
- **Fix.** `/boot` slash command shows the last BOOT.md run's output + timestamp.
- **Effort.** XS.
- **Priority.** **P3.**
- **Dependencies.** A3.

---

## Section D — Routing / config gaps

### D1. Bindings / routing invisible until they fire

- **What.** Bindings can pin model / profile per chat (`binding_resolver.py:1-15`). M3's `↪ routed:` badge fires only on routing-rule match (`dispatch.py:1742-1749`) and only once per session. Plain binding-driven profile resolution gets no badge.
- **Fix.** Persistent badge on every binding/routing turn (option), plus `/which` slash that returns the full resolution chain (bindings → routing → profile → model → mcp servers).
- **Effort.** S. ~2 days.
- **Priority.** **P1.**
- **Dependencies.** A3.

### D2. `enabled_plugins` filter is silent

- **What.** `agent_loop_factory.py:108-117` filters tools by `prof_cfg.enabled_plugins`. No discoverability surface for which tools the chat has.
- **Fix.** `/tools` slash command. CLI parity `oc tools list --platform telegram --chat-id <id>`.
- **Effort.** S. ~2 days.
- **Priority.** **P1.**
- **Dependencies.** A3.

### D3. No per-turn / per-session model override

- **Fix.** `/model <id>` per-session override. (Class `ModelPickerCommand` doesn't exist yet but `cli_model_picker.py` does — port the resolver.)
- **Effort.** S. ~3 days.
- **Priority.** **P2.**

### D4. MCP fleet is shared across all sessions in the gateway process

- **What.** `MCPManager` runs on a daemon-thread event loop, singleton-per-process. Profile rebind already rotates the fleet (CLAUDE.md gotcha 16). But all sessions in one process share the active fleet at any moment — no per-session isolation.
- **Fix.** Document the per-profile route (bindings → profile → its MCP set). For per-session in one profile, would need profile-cloning — out of scope.
- **Effort.** XS docs only.
- **Priority.** **P2.**

### D5. `BOOT.md`, `MEMORY.md`, `USER.md`, `DREAMS.md`, `SOUL.md` are profile-scoped, not chat-scoped

- **What.** Each profile has one of each. If you bind two Telegram chats to the same profile, they share memory. If you want chat-scoped memory, you need separate profiles.
- **Fix.** Document the trade-off. Architectural change (chat-scoped MEMORY.md) is XL — separate spec.
- **Priority.** **P3.**

### D6. `PII redaction` is opt-in and undiscoverable

- **What.** `privacy.redact_pii` config (`gateway/pii.py:1-12`) HMAC-hashes user/chat IDs before the LLM sees them. Opt-in. No slash command, no banner indicator.
- **Why it matters.** Users worried about leaking chat IDs to model providers (a real concern for shared chats) don't know this exists.
- **Fix.** `/privacy` slash showing redaction status + how to enable. Banner indicator when redaction is on.
- **Effort.** XS.
- **Priority.** **P3.**
- **Dependencies.** A3, A7.

---

## Section E — Observability / debuggability gaps

### E1. `oc gateway diagnose` requires daemon-machine shell access

- **Fix.** `gateway.diagnose.status` wire RPC + `/diagnose` slash command for chat-side access.
- **Effort.** S.
- **Priority.** **P3.**

### E2. No `/prompt` to inspect the rendered system prompt

- **What.** CLI `oc context show`. Gateway has nothing.
- **Fix.** `/prompt` returns a redacted summary of the last turn's system prompt.
- **Effort.** S.
- **Priority.** **P2.**

### E3. No tool-call audit visible in chat

- **What.** Every tool call is logged to `audit.db` immutable chain. User can `oc audit` from CLI; from Telegram there's no surface.
- **Fix.** `/audit` slash command for the last N tool calls of the current session.
- **Effort.** XS-S.
- **Priority.** **P3.**

### E4. Replay sanitization is a black-box on user restarts

- **What.** `gateway/replay_sanitizer.py` drops stale messages on gateway restart. User has no feedback that "X messages were sanitized at boot."
- **Evidence.** `replay_sanitizer.py:1-25`.
- **Fix.** Boot-time notification to home channel (if `sethome` is set): "Gateway restarted; sanitized N stale messages, replayed M."
- **Effort.** XS.
- **Priority.** **P3.**

---

## Section F — Multi-modal / attachment gaps

### F1. Outbound images/files via tool results are URL-only

- See B6.

### F2. Voice input transcribed; reply never spoken back

- **Fix.** `display.voice_response: auto | always | never` per-platform. Reply via `adapter.send_voice` when input was voice.
- **Effort.** S.
- **Priority.** **P3.**

### F3. PDF / docx / pptx / audio attachments not auto-routed to the right skill

- **What.** Attachment lands as a file path on the daemon FS. Agent receives the path but doesn't auto-trigger `ocr-and-documents` / `powerpoint` / `meeting-notes` skills.
- **Evidence.** `dispatch.py:715-960` — burst-merge attachments path. No mimetype-driven skill selection.
- **Fix.** On inbound attachment, detect mimetype; for known types auto-inject the extracted text into the next user message.
- **Effort.** M. ~1 week.
- **Priority.** **P2.**

### F4. Image attachments via the burst-merge path

- **What.** Multi-image bursts merge into one agent turn (`dispatch.py:451-478`). Working today. But the OCR / vision-analysis is at the agent's discretion — the agent must call `VisionAnalyze`. If the agent doesn't, the images go unused.
- **Fix.** Auto-call `VisionAnalyze` on inbound images and inject the description as user context. Opt-in `display.auto_describe_images`.
- **Effort.** S. ~3 days.
- **Priority.** **P3.**

### F5. Voice messages auto-transcribed; transcription is invisible

- **What.** Telegram voice transcribed to text before the agent sees it. The user has no way to see what was transcribed (Whisper transcription errors silently change the message).
- **Fix.** Append `[transcribed: "<text>"]` to the user's session view (not the agent's) so the user can verify the transcription.
- **Effort.** XS.
- **Priority.** **P3.**

---

## Section G — Architectural / foundational gaps

### G1. Two construction paths for `AgentLoop`

- **What.** `cli.py:1819` and `agent_loop_factory.py:108-125` build `AgentLoop` independently. Plus `AgentRouter` (`gateway/agent_router.py`) caches per-profile loops. Plus wire server has its own path. Four construction sites.
- **Fix.** Unify into `build_agent_loop(profile_home, source, **kwargs)`.
- **Effort.** L. 2-3 weeks.
- **Priority.** **P1.** Foundational; every other fix gets cheaper.

### G2. `RuntimeContext` populated inconsistently across paths

- **What.** CLI sets `plan_mode`, `yolo_mode`, `permission_mode` at construction. Gateway only sets `agent_context`/`custom`. Wire server has its own shape.
- **Fix.** `build_runtime_context(profile_home, source, slash_state, ...)` mirroring G1.
- **Effort.** S after G1.
- **Priority.** **P1**.

### G3. No behavioral parity regression test

- **What.** No end-to-end harness runs the same prompt through CLI + each adapter (mocked) and diffs the output.
- **Fix.** `tests/parity/test_behavioral.py` with 20 canonical prompts, response-length / tool-call / key-term checks; fail CI on >25% divergence per platform.
- **Effort.** L. ~2 weeks.
- **Priority.** **P1.**

### G4. `ChannelCapabilities` is flag-based, not versioned

- **Fix.** Bump version when adding a flag; old adapters get a synthesized v0 capabilities object. (Currently adding a flag just works because flag enum is purely additive — keep this property.)
- **Effort.** XS.
- **Priority.** **P3.**

### G5. Slash commands have no `gateway_safe` declaration today

- See A3. Tagging is mechanical; the architectural change is small. Calling out separately because it's the single biggest unlock.

### G6. `AgentRouter` cache leaks across config reload

- **What.** `agent_router.py` caches `profile_id → AgentLoop`. If the user reloads config (`/reload`) the cached loop still holds the old config until next process restart.
- **Evidence.** `gateway/agent_router.py:1-20`.
- **Fix.** Hook `oc config reload` to invalidate cached loops. (Wider: profile-rebind handlers already do this — verify hook firing.)
- **Effort.** S. Verification + plumbing.
- **Priority.** **P2.**

### G7. Hook coverage parity untested

- **What.** Gateway adds hooks the CLI doesn't have (`PRE_GATEWAY_DISPATCH`, `MESSAGE_SENDING`, channel adapter lifecycle hooks). Some plugins assume "if X event fires, both paths fire it" — true today but no test enforces.
- **Fix.** Test ensuring every `HookEvent` that fires on CLI also fires on gateway (or is documented as gateway-only / CLI-only).
- **Effort.** S.
- **Priority.** **P3.**

### G8. `outgoing_queue` cross-process bridge has no per-message audit

- **What.** `outgoing_queue.py` lets `oc mcp serve` (separate process) enqueue messages for the gateway to send. Sent without per-message audit-chain integration.
- **Fix.** Integrate audit chain so cross-process sends are traceable.
- **Effort.** S.
- **Priority.** **P3.**

---

## Section H — Adapter-specific gaps (per-platform)

These bite specific platforms only, not the architecture. Listed for completeness; fixes don't generalize.

### H1. Telegram — Markdown V2 escape edge cases on code blocks

- **Evidence.** `extensions/telegram/adapter.py:979` `_chunk_for_telegram` handles UTF-16 code units but escape edge cases for nested formatting are known per-Telegram-API.
- **Priority.** **P3.** Cosmetic.

### H2. Discord — 2000-char cap is stricter than Telegram's 4096; chunker handles, but embed limits differ

- **Fix.** Discord embed support for long replies (one embed = up to 4096 chars body).
- **Priority.** **P3.**

### H3. Slack — Bolt posts can't be edited like Telegram

- **Evidence.** `display_config.py:79` — Slack overrides `tool_progress: "off"` because edits would spam.
- **Priority.** Known; documented; no further action.

### H4. WhatsApp — Baileys bridge re-auth required periodically

- **Fix.** Boot-time notification when Baileys creds are stale.
- **Priority.** **P3.**

### H5. Email / Webhook — no streaming, no interactivity

- **By design.** Tier 4. No fix; the medium is async.

### H6. IRC — line-based, no Markdown, no images

- **By design.** Tier 3 with `streaming: False`.

### H7. iMessage / SMS — outbound only via macOS bridge or Twilio

- **Priority.** Adapter-quality; outside parity scope.

### H8. HomeAssistant — event-driven, not chat-driven

- **By design.** Tier 4.

---

## Section I — Cross-cutting

### I1. Gateway preflight refuses to start when another process owns the channel — but only at boot

- **Evidence.** `gateway/preflight.py:1-15` — kills bun/etc. competing for Telegram polling.
- **Gap.** A competing process started *after* the gateway is running silently steals the polling slot. Preflight only runs at boot.
- **Fix.** Periodic preflight check (every N minutes) with re-bind / re-acquire logic.
- **Effort.** S.
- **Priority.** **P3.** Edge case.

### I2. `/sethome` for cross-channel delivery is undocumented in chat

- **What.** `oc gateway sethome` writes `<profile>/gateway/home_channels.json` — designates a chat as the "default outbound" for cron / external delivery. Users learn this via CLI only.
- **Fix.** `/sethome` slash command + `/whoami` showing current home channels.
- **Effort.** S.
- **Priority.** **P3.**
- **Dependencies.** A3.

### I3. Channel directory of friendly names is populated but unused in chat

- **What.** `gateway/channel_directory.py` caches `(platform, chat_id) → display_name`. Used internally; never surfaced.
- **Fix.** `/channels` shows known channels with friendly names.
- **Effort.** XS.
- **Priority.** **P3.**

### I4. Reset policy (`/reset` / idle / daily) defaults differ from CLI's session lifecycle

- **What.** `gateway/reset_policy.py` modes: `off | idle | daily | both`. Default varies. CLI sessions end on exit; gateway sessions never end unless reset policy fires.
- **Fix.** Document; surface current policy via `/status`.
- **Effort.** XS.
- **Priority.** **P3.**

### I5. PII redaction salt is single-point-of-failure-on-loss

- **Evidence.** `pii.py:12-15` — `~/.opencomputer/.pii_salt`. Losing it un-correlates all hashed history.
- **Fix.** Bundle in profile backup; warn on first redaction-enabled boot if no backup.
- **Effort.** XS.
- **Priority.** **P3.**

### I6. Outgoing-queue retries do not back off on adapter failure

- **What.** `outgoing_drainer.py` marks failed on first error; no exponential backoff for transient.
- **Fix.** Backoff with N retries before terminal `mark_failed`.
- **Effort.** S.
- **Priority.** **P3.**

### I7. Mid-flight `agent_context` rebinding

- **What.** `agent_context="chat"` is set once per turn. A long turn that crosses into "cron-like" batch work (e.g. evolution/dreaming triggered) doesn't re-tag.
- **Fix.** Document; not a parity gap so much as an edge case.
- **Priority.** **P3.**

### I8. Ambient sensor daemon does not contribute to gateway turn context

- **Evidence.** `gateway/server.py:270-272` starts ambient daemon. Daemon writes to `ambient/state.json`. Gateway dispatcher does not read it back into turn context.
- **Fix.** Inject `ambient_context` (foreground app, recent files) into `RuntimeContext.custom` on gateway turn-build, parity with CLI.
- **Effort.** S.
- **Priority.** **P2.** Persona classifier on gateway is reading less context than on CLI.

---

## Section J — Honest residual (won't close)

### J1. Chat medium vs keyboard medium

CLI register is "two people at a terminal debugging together." Gateway register is "asking a friend a question while walking." The medium drives the register. No flag changes this.

### J2. Gateway sessions are long; CLI sessions are short

Telegram chats live for months. CLI sessions die on terminal close. Compaction over long sessions necessarily loses specificity. C1's fixes help; they don't eliminate.

### J3. Terminal rendering is fundamentally richer than chat

Rich tables, syntax-highlighted diffs, live progress bars. The fix-budget for matching this in chat is infinite; the practical ceiling is "make chat as good as chat can be."

---

## Execution roadmap

### Wave 1 — P0 (unblockers, ~3-4 weeks)

| # | Gap | Effort | Notes |
|---|---|---|---|
| 1 | **A6 — Per-chat `cwd`** | S | Without this, file tools are useless. |
| 2 | **A3 — `gateway_safe` tag on slash commands** | M | Unlocks ~12 items below. |
| 3 | **A1 — Wire streaming into dispatch** | M | Infra exists; wiring only. |

### Wave 2 — P1 (~6-8 weeks)

| # | Gap | Effort | Depends |
|---|---|---|---|
| 4 | **A9 — `collect` queue mode default for chat** | XS | — |
| 5 | **A7 — Session banner** | XS | — |
| 6 | **B3 — Capability slot in prompt** | S | — |
| 7 | **C3 — Per-chat `register` override** | S | — |
| 8 | **D1 — `/which` + persistent badges** | S | A3 |
| 9 | **D2 — `/tools` + CLI parity** | S | A3 |
| 10 | **A2 — `/plan on\|off`** | S | A3 |
| 11 | **B1 — `max_tokens` auto-continue** | S | — |
| 12 | **C1.1 — `/pin`** | S | A3 |
| 13 | **A4 — `/cancel` alias + bindings queue_mode** | S | A3 |
| 14 | **A5 — Async consent state machine** | L | — |
| 15 | **I8 — Ambient context to gateway turn** | S | — |
| 16 | **G1+G2 — Unified `build_agent_loop` / `RuntimeContext`** | L | — |
| 17 | **G3 — Behavioral parity test harness** | L | — |

### Wave 3 — P2 (~6 weeks)

| # | Gap | Effort |
|---|---|---|
| 18 | B2 — Reasoning visibility on Tier 1 | S |
| 19 | B4 — Per-platform Markdown rendering | M |
| 20 | B5 — Compact tool-progress mode | S |
| 21 | B6 / F1 — Outbound media via `send_image` / `send_document` | M |
| 22 | F3 — Auto-skill route for PDF/docx | M |
| 23 | C1.2 — Compaction-triggered MEMORY writes | M |
| 24 | C1.3 — Auto-fork at N turns | M |
| 25 | C4 — `/resume <id>` | S |
| 26 | D3 — `/model <id>` per-session | S |
| 27 | A8 — `/handoff` on gateway | XS |
| 28 | E2 — `/prompt` | S |
| 29 | G6 — `AgentRouter` cache invalidation on reload | S |

### Wave 4 — P3 (niche)

| # | Gap | Effort |
|---|---|---|
| 30 | E1 — Wire RPC for `diagnose` | S |
| 31 | E3 — `/audit` | XS-S |
| 32 | E4 — Sanitizer boot notification | XS |
| 33 | F2 — Voice response | S |
| 34 | F4 — Auto vision-analyze on inbound | S |
| 35 | F5 — Show transcription to user | XS |
| 36 | C5 — `/mirrors` | XS |
| 37 | C6 — `/boot` | XS |
| 38 | D4 — MCP fleet docs | XS |
| 39 | D5 — Chat-scoped MEMORY docs | XS |
| 40 | D6 — `/privacy` | XS |
| 41 | G4 — Capability version | XS |
| 42 | G7 — Hook coverage test | S |
| 43 | G8 — Cross-process send audit | S |
| 44 | H1-H8 — Per-platform polish | varies |
| 45 | I1 — Periodic preflight | S |
| 46 | I2 — `/sethome` | S |
| 47 | I3 — `/channels` | XS |
| 48 | I4 — Reset policy in `/status` | XS |
| 49 | I5 — PII salt backup warning | XS |
| 50 | I6 — Outgoing-queue backoff | S |

---

## Diagnostic — run these before executing

```bash
# 1. Mechanisms firing on your install
oc gateway diagnose --rollup --since 30d

# 2. Profile comparison
oc profile list
ls -la ~/.opencomputer/default/{MEMORY,USER,SOUL,DREAMS,BOOT}.md
wc -l ~/.opencomputer/default/MEMORY.md \
       ~/.opencomputer/default/USER.md

# 3. Resolve which profile + model + cwd your chat hits
oc bindings list
oc routing test --platform telegram --chat-id <id>
oc -p default model get
oc -p <gateway-profile> model get

# 4. Daemon's working directory
ps aux | grep "oc gateway"     # find PID
lsof -p <pid> | grep cwd       # daemon's cwd

# 5. Adapter capabilities
oc adapter capabilities

# 6. Slash command surface
ls opencomputer/agent/slash_commands_impl/

# 7. Telemetry rollup
sqlite3 ~/.opencomputer/<profile>/audit.db \
  "SELECT mechanism_id, COUNT(*) turns, SUM(fired) fired,
          ROUND(100.0*SUM(fired)/COUNT(*),1) pct
   FROM gateway_parity_log
   WHERE ts > strftime('%s','now','-30 days')
   GROUP BY mechanism_id ORDER BY fired DESC;"

# 8. Outgoing queue health
sqlite3 ~/.opencomputer/<profile>/gateway/outgoing_messages.db \
  "SELECT status, COUNT(*) FROM outgoing_messages GROUP BY status;"

# 9. Persistent home channels (sethome targets)
cat ~/.opencomputer/<profile>/gateway/home_channels.json 2>/dev/null

# 10. Channel directory cache
cat ~/.opencomputer/channel_directory.json 2>/dev/null | head -50
```

---

## Honest closing note

This is the second version. The first was missing four pieces of existing infrastructure (`BlockChunker`, `QueueManager`, `ChannelCapabilities`, unified `SlashCommand` system). After auditing, **most "missing features" are actually unwired features** — the engineering cost is lower than v1 suggested, and the architectural picture is healthier than I implied.

Items that genuinely require new work:
- Async consent state machine (A5) — turn-state serialization does not exist
- Unified `build_agent_loop` (G1) — real refactor
- Behavioral parity test harness (G3) — new test surface
- Tool-result media interceptor (B6) — new dispatch layer
- Per-platform render rules (B4) — new module
- Compaction-triggered MEMORY writes (C1.2) — new dreaming-v2 trigger

Everything else is **wiring**: declare `gateway_safe=True`, plumb a config field, register an alias, inject a runtime context value. Cheap individually. The compound effect of doing all of Wave 1 + Wave 2 is what closes the gap.

After Wave 1+2: gateway will be ~95% of CLI for single-turn questions, ~90% for multi-tool tasks. Long-session degradation (J2) and chat-vs-terminal richness (J3) are the floor.

This doc is a map. Verify each gap against current main before fixing — the audit is dated 2026-05-18 and the repo moves quickly. The `oc gateway diagnose --rollup` output should drive ordering inside each priority bucket; the priority bucket itself is fixed.

If you find more gaps not in this doc, that's expected — gateway-vs-CLI is a continuous surface, not a finite enum. Append them to `Section A-I` as appropriate. Don't let "perfect doc" block execution of Wave 1.
