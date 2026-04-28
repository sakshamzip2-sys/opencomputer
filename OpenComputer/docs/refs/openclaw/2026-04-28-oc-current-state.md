# OpenComputer current-state survey (2026-04-28)

Replaces the stale `docs/refs/openclaw/inventory.md` (2026-04-22). Captures what is shipped on `main` so the OpenClaw gap can be re-computed against today's surface — Hermes Tier 1+2+3 ports, layered awareness V2.B+V2.C, voice mode, browser control, ambient sensors, plus the in-flight Skills Hub PR set.

---

## 1. Repo snapshot

- Branch: `main`
- Tip SHA: `4db74443d58d116e4bf446f8119318448b10ea2a`
- Tip subject: `feat: hermes channel feature port — Tier 1+2+3 (Matrix E2EE dropped) (#221)`
- Test files: **426** (`find tests -name 'test_*.py'`)
- Test LOC: ~88,883 lines across the suite
- 9 open PRs (numbers `#220`-`#228`) — see §7
- Working tree: clean
- 27 directories under `extensions/` (was 7 in CLAUDE.md §2; pre-Hermes-port count)

---

## 2. Extensions catalog (27 plugins)

`extensions/` contains 27 plugin directories on `main`. All declare `kind` in `plugin.json` (no YAML manifests — see CLAUDE.md gotcha §5). Grouped by kind:

### Providers (5)

| Plugin | kind | Description | Source |
|---|---|---|---|
| `anthropic-provider` | provider | Native Anthropic + Bearer-proxy router support; prompt-caching aware | claude-code derived |
| `openai-provider` | provider | OpenAI + OpenAI-compatible endpoints | claude-code derived |
| `aws-bedrock-provider` | provider | Bedrock client + transport (Sub-project F-adjacent) | built fresh |
| `memory-honcho` | provider | Honcho-as-memory-provider; AGPL container, never vendored | hermes derived |
| `weather-example` | provider | Reference example for plugin authors | scaffolder docs |

### Channel adapters (12)

| Plugin | kind | Description | Source |
|---|---|---|---|
| `telegram` | channel | Bot API + DM Topics + MarkdownV2 + sticker cache + entity-mention boundaries | hermes Tier-1+2 ported |
| `discord` | channel | Mentions, allowed_mentions safe defaults, forum threads, full slash command tree | hermes Tier-1+2 ported |
| `slack` | channel | mrkdwn + Block Kit ConsentGate inline approval + signature-verified webhook | hermes Tier-1+2 ported |
| `matrix` | channel | HTML formatted_body + retry (E2EE explicitly dropped) | hermes Tier-1 ported |
| `mattermost` | channel | Retry wiring + lifecycle hooks | hermes Tier-1 ported |
| `signal` | channel | Retry + phone redaction in logs | hermes Tier-1 ported |
| `sms` | channel | SMS gateway adapter, retry + phone redaction | hermes Tier-1 ported |
| `imessage` | channel | macOS BlueBubbles bridge | hermes adapter style |
| `whatsapp` | channel | WhatsApp Cloud API + format converter + retry | hermes Tier-1 ported |
| `whatsapp-bridge` | channel | NEW: Baileys subprocess + QR login + cross-platform process kill | PR #221 |
| `email` | channel | IMAP/SMTP adapter; automated-sender filter (NoReply/Auto-Submitted) | hermes Tier-1 ported |
| `homeassistant` | channel | Home Assistant chat bridge | hermes Tier-1 ported |
| `webhook` | channel | `deliver_only` + `cross_platform` modes; HMAC-SHA256; idempotency cache | hermes Tier-1 ported |
| `api-server` | channel | OpenAI-compatible inference HTTP API exposing the agent | claude-code derived |

### Tool plugins (3)

| Plugin | kind | Description | Source |
|---|---|---|---|
| `dev-tools` | tool | `GitDiffTool`, `BrowserTool`, `FalTool` (porcelain dev utilities) | openclaw inspired |
| `browser-control` | tool | NEW: 5 Playwright browser tools (navigate/click/fill/snapshot/scrape) | PR #202 (built fresh) |
| `browser-bridge` | tool | Browser bridge (legacy / lighter-weight) | claude-code derived |

### Mixed plugins (5)

| Plugin | kind | Description | Source |
|---|---|---|---|
| `coding-harness` | mixed | EditTool, MultiEditTool, TodoWriteTool, ExitPlanMode, StartProcess/CheckOutput/KillProcess, RewindTool, CheckpointDiff, RunTests + plan-mode + accept-edits + 6 hooks + 3 skills + introspection (psutil/mss/OCR) | claude-code derived + native rewrite |
| `skill-evolution` | mixed | Pattern detection, skill candidate extractor, SessionMetrics adapter | PR #193/#204 (built fresh) |
| `voice-mode` | mixed | Continuous push-to-talk: capture → VAD → STT → agent → TTS; local Whisper fallback | PR #199 (built fresh) |
| `ambient-sensors` | mixed | Cross-platform foreground-app sensor (mac/linux/win); hashed titles | PR #184 (built fresh) |
| `affect-injection` | mixed | DynamicInjectionProvider surfacing `<user-state>` block (vibe + arc + active life-event pattern) | commit `13558b29` (built fresh) |

### Empty stubs

| Plugin | Status |
|---|---|
| `oi-capability` | Stub directory (only `__pycache__/`) — likely retired during PR #179 OI-removal |

---

## 3. Core agent map (`opencomputer/`)

### `agent/` — agent loop subsystems

`loop.py` (2,598 LOC), `state.py` (SessionDB + FTS5), `memory.py` (declarative, 535 LOC), `episodic.py` (140 LOC), `dreaming.py` (495 LOC), `recall_synthesizer.py` (236 LOC), `compaction.py` (300 LOC). Plus: `agent_cache.py`, `agent_templates.py`, `auxiliary_client.py` (Hermes pattern), `bg_notify.py`, `budget_config.py`, `cheap_route.py`, `compaction.py`, `config.py`, `config_store.py`, `consent/` (F1 layer), `context_engine.py` + `context_engine_registry.py`, `credential_pool.py`, `fallback.py`, `injection.py`, `injection_providers/` (`link_summary.py`), `link_understanding.py`, `memory_bridge.py`, `memory_context.py`, `model_metadata.py`, `profile_config.py`, `prompt_builder.py`, `prompt_caching.py`, `prompts/`, `rate_guard.py`, `reviewer.py` (post-response), `slash_commands.py` + `slash_commands_impl/` + `slash_dispatcher.py`, `steer.py`, `step.py`, `subdirectory_hints.py`, `title_generator.py`, `tool_ordering.py`, `tool_result_storage.py`, `vibe_classifier.py`, `workspace.py`.

### `tools/` — built-in (non-plugin) tools

24 tool source files. See §4 table.

### `awareness/`

- `learning_moments/` — engine + predicates + registry + store (passive-education v2)
- `life_events/` — pattern + registry + 6 detectors (`burnout`, `exam_prep`, `health_event`, `job_change`, `relationship_shift`, `travel`)
- `personas/` — classifier + registry + `defaults/` + `_foreground.py`

### `voice/`

`__init__.py`, `costs.py`, `stt.py`, `tts.py` — cost-guarded TTS/STT module the `VoiceSynthesizeTool`/`VoiceTranscribeTool` wrap.

### Other top-level subsystems

| Path | Files | Description |
|---|---|---|
| `acp/` | `server.py`, `session.py`, `tools.py` | Agent Control Protocol (claude-code parity) |
| `agents/` | `code-reviewer.md` | Bundled subagent definitions |
| `channels/` | `__init__.py` only | Reserved namespace |
| `cli_ui/` | 11 files (input_loop, slash, slash_completer, slash_handlers, streaming, turn_cancel, resume_picker, clipboard, empty_state, keyboard_listener) | TUI uplift Phase 1+2 (PRs #180, #200, #207, #210-#216) |
| `consent/` | `pairing.py` | Channel-pairing consent flow |
| `cost_guard/` | `guard.py` | Token + spend caps |
| `cron/` | `jobs.py`, `scheduler.py`, `threats.py` | G.1 cron-jobs subsystem |
| `dashboard/` | `server.py`, `static/` | Local dashboard |
| `ensemble/` | `persona_command.py`, `switcher.py` | Persona plurality |
| `evolution/` | 19 files | Skill-evolution core (`pattern_detector`, `pattern_synthesizer`, `procedural_memory_loop`, `prompt_evolution`, `quarantine_writer`, `reflect`, `reward`, `synthesize`, `trajectory`, etc.) |
| `gateway/` | `server.py`, `dispatch.py`, `protocol.py`/`protocol_v2.py`, `wire_server.py`, `outgoing_queue.py` + `outgoing_drainer.py`, `channel_directory.py` | Gateway daemon + WebSocket wire server |
| `hooks/` | `engine.py`, `runner.py`, `shell_handlers.py` | Hook dispatcher; settings-based shell hooks |
| `inference/` | `engine.py`, `extractors/`, `storage.py` | F2 motif inference bus |
| `ingestion/` | `bus.py` | F2 signal bus |
| `mcp/` | `client.py`, `oauth.py`, `oauth_pkce.py`, `osv_check.py`, `presets.py`, `server.py` | MCP integration (deferred load + OSV malware scan) |
| `observability/` | `logging_config.py` | Loguru-style config |
| `plugins/` | `discovery.py`, `loader.py`, `registry.py`, `manifest_validator.py`, `preset.py`, `security.py`, `demand_tracker.py` | Plugin system (not plugins themselves) |
| `profile_bootstrap/` | 17 files | F4-adjacent: orchestrator, Spotlight, calendar/browser-history/idle/embedding/raw_store/vector_store/quick_interview/identity_reflex |
| `release/` | `version.py` | Version pin |
| `sandbox/` | `auto.py`, `docker.py`, `linux.py`, `macos.py`, `none_strategy.py`, `runner.py`, `ssh.py` | F3 sandbox runners |
| `security/` | `env_loader.py`, `instruction_detector.py`, `osv_check.py`, `python_safety.py`, `sanitize.py`, `scope_lock.py`, `url_safety.py` | Tier-S OSV + URL/SSRF + instruction injection guards |
| `settings_variants/` | `lax.yaml`, `sandbox.yaml`, `strict.yaml` | III.3 starter configs |
| `skills/` | 55 directories with `SKILL.md` files | Bundled skills (superpowers + everything-claude-code subset + native ones — meeting-notes, inbox-triage, bill-deadline-tracker, coding-via-chat, etc.) |
| `skills_guard/` | `policy.py`, `scanner.py`, `threat_patterns.py` | Skills-Guard threat scanner |
| `skills_hub/` | only `__pycache__/` artifacts; **NO source files on `main`** | Hub belongs to PR #220 (open). Stale `.pyc` left over from a stash. |
| `system_control/` | `bus_listener.py`, `logger.py`, `menu_bar.py` | F-series system-control bridge |
| `tasks/` | `runtime.py`, `store.py` | Spawn-detached-task subsystem |
| `templates/plugin/` | scaffolder template | Sub-project B `opencomputer plugin new` |
| `user_model/` | 9 files | F4 user-model graph: `context.py`, `decay.py`, `drift.py`, `drift_store.py`, `honcho_bridge.py`, `importer.py`, `scheduler.py`, `store.py` |

### CLI surfaces

`cli.py` is the Typer entrypoint. 31 cli_*.py modules wire subcommands: `cli_adapter`, `cli_ambient`, `cli_audit`, `cli_awareness`, `cli_channels`, `cli_consent`, `cli_cost`, `cli_cron`, `cli_dashboard`, `cli_help`, `cli_hints`, `cli_inference`, `cli_insights`, `cli_mcp`, `cli_memory`, `cli_models`, `cli_pair`, `cli_plugin`, `cli_plugin_scaffold`, `cli_preset`, `cli_profile`, `cli_sandbox`, `cli_security`, `cli_session`, `cli_skills`, `cli_system_control`, `cli_task`, `cli_telegram`, `cli_update_check`, `cli_user_model`, `cli_voice`, `cli_webhook`. Top-level commands include `chat`, `gateway`, `wire`, `code` (V3.A coding harness), `setup`, `doctor`, `config show/init/variants`, `plugins`, `skills`.

---

## 4. Plugin SDK (`plugin_sdk/`)

24 public modules. Public exports per `plugin_sdk/__init__.py`:

| Module | Major exports |
|---|---|
| `core.py` | `Message`, `MessageEvent`, `ModelSupport`, `Platform`, `PluginActivationSource`, `PluginManifest`, `PluginSetup`, `ProcessingOutcome`, `Role`, `SendResult`, `SetupChannel`, `SetupProvider`, `SingleInstanceError`, `StopReason`, `ToolCall`, `ToolResult` |
| `tool_contract.py` | `BaseTool`, `ToolSchema` |
| `provider_contract.py` | `BaseProvider`, `ProviderResponse`, `StreamEvent`, `Usage` |
| `channel_contract.py` | `BaseChannelAdapter`, `ChannelCapabilities` |
| `channel_helpers.py` | `_send_with_retry`, lifecycle hooks (`on_processing_*`), file/media extraction |
| `channel_utils.py`, `network_utils.py` | Phone redaction, IP-fallback transport, retry helpers |
| `format_converters/` | MarkdownV2 / Slack mrkdwn / Matrix HTML / WhatsApp formatters |
| `sticker_cache.py` | Telegram sticker vision cache |
| `file_lock.py` | flock for profile.yaml writes |
| `classifier.py` | `Classifier`, `RegexClassifier`, `Rule`, `AggregationPolicy`, `ClassifierVerdict` |
| `consent.py` | `CapabilityClaim`, `ConsentDecision`, `ConsentGrant`, `ConsentTier` (F1) |
| `decay.py` | `DecayConfig`, `DriftConfig`, `DriftReport` |
| `doctor.py` | `HealthContribution`, `HealthRunFn`, `HealthStatus`, `RepairResult` |
| `hooks.py` | `HookContext`, `HookDecision`, `HookEvent`, `HookHandler`, `HookSpec`, `ALL_HOOK_EVENTS` (12 events: PreToolUse, PostToolUse, Stop, SessionStart/End, UserPromptSubmit, PreCompact, SubagentStop, Notification, PreLLMCall, PostLLMCall, TransformToolResult) |
| `inference.py` | `Motif`, `MotifExtractor`, `MotifKind` |
| `ingestion.py` | `SignalEvent`, `MessageSignalEvent`, `ToolCallEvent`, `FileObservationEvent`, `WebObservationEvent`, `HookSignalEvent`, normalizers |
| `injection.py` | `DynamicInjectionProvider`, `InjectionContext` |
| `interaction.py` | `InteractionRequest`, `InteractionResponse` |
| `memory.py` | `MemoryProvider` |
| `runtime_context.py` | `RuntimeContext`, `RequestContext`, `DEFAULT_RUNTIME_CONTEXT` |
| `sandbox.py` | Sandbox abstractions |
| `slash_command.py` | `SlashCommand` base + result type |
| `tool_matcher.py`, `transports.py`, `user_model.py` | Internal helpers |

Boundary enforced by `tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer`.

---

## 5. Tool registry — what's wired

### Core (registered in `opencomputer/cli.py::_register_builtin_tools`, ~24 tools)

| PascalCase name | Module | Purpose |
|---|---|---|
| `Read` | `tools/read.py` | Read file (with PDF/notebook modes) |
| `Write` | `tools/write.py` | Write/overwrite file |
| `Bash` | `tools/bash.py` | Shell command (with safety) |
| `PythonExec` | `tools/python_exec.py` | Sandboxed Python interpreter (V3.A) |
| `Grep` | `tools/grep.py` | ripgrep-style search |
| `Glob` | `tools/glob.py` | filesystem glob |
| `SkillManage` | `tools/skill_manage.py` | Self-improvement: agent saves skills |
| `Skill` | `tools/skill.py` | Skill invocation tool (Claude-code parity) |
| `Delegate` | `tools/delegate.py` | Spawn subagent with isolated context |
| `WebFetch` | `tools/web_fetch.py` | URL fetcher (SSRF guard, allowlist) |
| `WebSearch` | `tools/web_search.py` | Multi-provider search dispatch |
| `NotebookEdit` | `tools/notebook_edit.py` | Jupyter notebook cell edit |
| `PushNotification` | `tools/push_notification.py` | OS notification |
| `AskUserQuestion` | `tools/ask_user_question.py` | Interactive ask-user |
| `Recall` | `tools/recall.py` | Episodic memory recall |
| `Memory` | `tools/memory_tool.py` | Declarative memory write/read |
| `Cron` | `tools/cron_tool.py` | G.1 cron jobs (capability-claimed) |
| `VoiceSynthesize` | `tools/voice_synthesize.py` | TTS tool |
| `VoiceTranscribe` | `tools/voice_transcribe.py` | STT tool |
| `SessionSearch` | `tools/session_search_tool.py` | Search prior sessions (FTS5) |
| `SpawnDetachedTask` | `tools/spawn_detached_task.py` | Long-running detached task |
| `PointAndClick`* | `tools/point_click.py` | macOS-only mouse click |
| `AppleScriptRun`* | `tools/applescript_run.py` | macOS-only AppleScript |
| `PointToClick` (PtC) | `tools/ptc.py` | macOS GUI helper |

\* macOS-only — gated by `sys.platform == "darwin"` in cli.py.

### Plugin-registered

| Plugin | Tools |
|---|---|
| `coding-harness` | `Edit`, `MultiEdit`, `TodoWrite`, `ExitPlanMode`, `StartProcess`, `CheckOutput`, `KillProcess`, `Rewind`, `CheckpointDiff`, `RunTests` (+ introspection tools registered conditionally) |
| `dev-tools` | `GitDiff`, `Browser`, `Fal` |
| `browser-control` | 5 Playwright tools (navigate, click, fill, snapshot, scrape) |
| `browser-bridge` | bridge tool |

Tool-name uniqueness is the collision guard — `ToolRegistry` raises `ValueError` on name conflict.

---

## 6. Slash commands

Slash commands dispatch via `opencomputer/agent/slash_commands.py` + `slash_dispatcher.py`. Plugin-registered commands flow through the same `_plugin_registry.slash_commands` dict.

### Built-in (core)

| Command | Source | Purpose |
|---|---|---|
| `/scrape` | `agent/slash_commands_impl/scrape.py` | V3.A profile-scraper skill |

(Only one core built-in on `main` today. The full Tier 2.A 6-command bundle lives in PR #223.)

### Plugin-registered (coding-harness)

| Command | Class | Purpose |
|---|---|---|
| `/checkpoint` | `CheckpointCommand` | Save explicit rewind checkpoint |
| `/diff` | `DiffCommand` | Show last edit diff |
| `/undo` | `UndoCommand` | Roll back last checkpoint |
| `/plan` | `PlanOnCommand` | Enter plan mode |
| `/plan-off` | `PlanOffCommand` | Leave plan mode |
| `/accept-edits` | `AcceptEditsCommand` | Accept-edits mode toggle |

### Pending (in PRs #220-#228, NOT on main)

`/copy`, `/yolo`, `/reasoning`, `/fast`, `/usage`, `/platforms` (#223), `/<skill-name>` auto-dispatch (#225), `/snapshot`, `/rollback`, `/queue`, `/reload` (Tier 2.A continued, not yet open).

---

## 7. Subsystem status table

| Subsystem | Status | Notes |
|---|---|---|
| Honcho memory overlay | shipped | `extensions/memory-honcho/` — provider, AGPL container pulled at install, per-profile host key (Phase 14.J) |
| SQLite + FTS5 sessions | shipped | `agent/state.py::SessionDB` |
| Episodic memory | shipped | `agent/episodic.py` (140 LOC) + `RecallTool` |
| Declarative memory | shipped | `agent/memory.py::MemoryManager` (535 LOC) |
| Procedural memory loop | shipped | `evolution/procedural_memory_loop.py` |
| Skill evolution (auto-detect+extract) | shipped | PR #193 + #204 (SessionDB→SessionMetrics adapter) |
| Skills Hub | **in-flight** | PR #220 open. `opencomputer/skills_hub/` has no source files on `main`; only stale `__pycache__/` from a stash |
| Active memory / dream / sleep | shipped | `agent/dreaming.py` (495 LOC) + `agent/recall_synthesizer.py` (236 LOC) |
| Compaction | shipped | `agent/compaction.py` (300 LOC) |
| Voice mode (push-to-talk) | shipped | PR #199 — `extensions/voice-mode/` |
| Edge TTS provider | **in-flight** | PR #227 open |
| Groq STT provider | **in-flight** | PR #228 open |
| Local Whisper STT | shipped | `extensions/voice-mode/stt.py` (mlx-whisper / whisper-cpp fallbacks) |
| OpenAI Whisper STT | shipped | Built into voice-mode plugin |
| Browser control (Playwright) | shipped | PR #202 — `extensions/browser-control/` |
| Layered Awareness MVP | shipped | PR #143 (Layers 0/1/2 + Layer 4 minimal) |
| Layered Awareness V2.B (deepening) | shipped | PR #155 — `profile_bootstrap/deepening.py` + Ollama + BGE/Chroma + Spotlight + idle |
| Layered Awareness V2.C (life events + personas) | shipped | PR #163 — 6 life-event detectors + 5 plural personas |
| Affect work A/B/C | shipped | commit `13558b29` — vibe classifier + affect-injection plugin + tone |
| Companion voice spec | shipped | `docs/superpowers/specs/2026-04-27-companion-voice-examples.md` |
| Pluggable Layer-3 extractor | shipped | PR #208 (Ollama / Anthropic / OpenAI) |
| Smart-fallback Ollama-missing prompt | shipped | PR #209 |
| Passive education v1+v2 | shipped | PRs #213, #218, #219 |
| Tier-S port (caching, OSV, SSRF, instruction-detector, async titler) | shipped | PR #171 |
| Hermes channel feature port | shipped | PR #221 (the megamerge) |
| Ambient foreground sensor | shipped | PR #184 |
| Auto skill evolution | shipped | PR #193 + #204 |
| F1 Consent layer | shipped | `agent/consent/` + `plugin_sdk/consent.py` (PR #64) |
| F2 Inference + ingestion bus | shipped | `inference/`, `ingestion/` (PR #68/#69) |
| F3 Sandbox runners | shipped | `sandbox/` — auto, docker, linux, macos, none, ssh |
| F4 User-model graph | shipped | `user_model/` (PR #71) |
| F5 Decay/drift | shipped | `user_model/decay.py`, `drift.py`, `drift_store.py` (PR #74) |
| F6 System-control bus | shipped | `system_control/` (PR #79) |
| Native introspection (replaces OI bridge) | shipped | PR #179 — `extensions/coding-harness/introspection/` |
| Cron jobs | shipped | `cron/` + `CronTool` |
| MCP integration | shipped | `mcp/` — install-from-preset, OAuth PKCE, OSV check |
| TUI Phase 1+2 | shipped | PRs #180, #200, #207, #210-#216 (PromptSession, slash autocomplete, dropdown, resume picker, thinking spinner, session-title indicator) |
| Demand-driven plugin activation | shipped | Sub-project E (PR #26) |
| Profile parity with Hermes (`home/`, `SOUL.md`) | shipped | Sub-project C (PR #24) |
| ACP server | shipped | `acp/` — claude-code parity |
| Webhook gateway | shipped | `cli_webhook` + `extensions/webhook` (deliver_only, cross_platform) |
| API server (OpenAI-compatible) | shipped | `extensions/api-server/` |
| Dashboard | shipped | `dashboard/server.py` + static |
| OpenClaw-style typed gateway protocol | partial | `gateway/protocol.py` + `protocol_v2.py` exist; no per-domain schema split |
| OpenClaw memory-lancedb / memory-wiki | missing | Tier-3 in old inventory; not started |
| OpenClaw image/video/music gen | missing | "skip" verdict in old inventory |
| OpenClaw msteams / googlechat / line / irc / twitch / synology / nextcloud | missing | "skip" verdict; not pursued |
| OpenClaw extra providers (azure / google / mistral / xai / deepseek / qwen / moonshot / together / openrouter / groq / lmstudio / ollama) | mostly missing | only AWS Bedrock added |

---

## 8. In-flight PRs (#220-#228)

All open per `gh pr list`:

| # | Branch | Title | Status |
|---|---|---|---|
| 220 | `feat/skills-hub` | Skills Hub MVP + agentskills.io standard (Tier 1.A) | open |
| 222 | `feat/first-class-tools` | 4 first-class generative tools (Tier 1.B) | open |
| 223 | `feat/tier2-slash-commands` | 6 self-contained slash commands `/copy /yolo /reasoning /fast /usage /platforms` (Tier 2.A) | open |
| 224 | `feat/provider-runtime-flags` | Wire `/reasoning` + `/fast` slash through to provider API kwargs | open |
| 225 | `feat/slash-skill-fallback` | `/<skill-name>` auto-dispatch — every installed skill reachable as a slash | open |
| 226 | `feat/tier2b-tui-polish` | Bell-on-complete + external editor (Ctrl+X Ctrl+E) (Tier 2.B) | open |
| 227 | `feat/edge-tts` | Edge TTS — free voice provider, no API key required | open |
| 228 | `feat/groq-stt` | Tier 3.E — Groq STT (fast/cheap Whisper transcription) | open |

PR #221 (Hermes channel feature port — the megamerge) is the one shipped on `main`. There is no #228+ track open beyond Groq STT yet. Per CLAUDE.md memory, Tier 2.A continuation (`/snapshot /rollback /queue /reload`), Tier 2.B (`@filepath`/worktree), Tier 3 (Groq STT/PII/Tirith), and OpenClaw remain unstarted.

Stash list shows `stash@{0}: skills-hub doc tweak` — confirms skills_hub source is parked in the open PR's branch, not on `main`.

---

## 9. Recent commit timeline (most recent 25)

```
4db74443 feat: hermes channel feature port — Tier 1+2+3 (Matrix E2EE dropped) (#221)
9a955b47 docs(audit): skills/tools/plugins audit + privacy & risk-register report
13558b29 feat(awareness): affect work A/B/C — cross-persona vibe + injection plugin + tone
1d2d149c feat(cli): passive-education v2 — empty-state pass + failure teach + oc help tour (#219)
75885a98 feat(awareness): passive-education v2 — mechanisms B + C + 3 new moments (#218)
7c66c302 feat(awareness): passive-education learning-moments v1 (#213)
ee3a2913 feat(awareness): smart-fallback prompt when Ollama missing + cloud key set (#209)
6e3078c4 feat(awareness): pluggable Layer 3 extractor (Ollama / Anthropic / OpenAI) (#208)
f6ca107b fix(prompt): companion overlay was being neutered by base.j2 (#217)
2bf4eac4 fix(tui): force enable_cpr=False on the input Output — kills CPR path entirely (#216)
6f0d935c fix(tui): disable auto-titler + filter long titles from corner indicator (#215)
73954c18 fix(awareness): Layer 2 scans now populate user-model graph (#206)
84e1da67 fix(awareness): vibe classifier — detach from companion gate + per-turn log (Path A) (#205)
42451d0c fix(tui): dropdown ABOVE input + session-title corner indicator (#214)
68dce215 feat(tui): polish resume picker + 21 hard E2E tests for selection flow (#212)
1de5ce82 fix(tui): Dimension.exact(N) classmethod, not Dimension(exact=N) kwarg (#211)
4031604a fix(tui): visible dropdown + chat resume + history render + thinking spinner (#210)
73bfb5c0 feat(tui): visible dropdown in editor terminals + oc resume picker (#207)
0c81547c fix(skill-evolution): SessionDB → SessionMetrics adapter (production gap) (#204)
bdf4b332 feat(tui): slash command autocomplete (Phase 2) (#200)
a716b253 feat(skills): T2 batch — meeting-notes, inbox-triage, bill-deadline-tracker, coding-via-chat (#203)
c6a7ebd5 feat: browser control via Playwright (T1.C) (#202)
00fdc58a feat(plugin_sdk): RegexClassifier abstraction + vibe/JobChange migration (#201)
30769b47 feat(voice-mode): continuous push-to-talk + local-Whisper fallback (T1.B) (#199)
b1c457e3 docs(plan): A.6 V2.D stretch — PARKED, not deferred (#198)
```

---

## 10. Pre-existing OpenClaw notes inventory

| File | Date | Coverage |
|---|---|---|
| `docs/refs/openclaw/inventory.md` | 2026-04-22 | OpenClaw → OC parity table: tools, channels, providers, search, knowledge/memory, media, exec/sandbox, dev tools, diagnostics, MCP, plugin SDK, gateway. Last column lists `verdict` (port/already-have/skip/port-later) and `Destination`. **Stale** — predates everything from PR #143 onward (layered awareness, voice, browser, ambient sensors, channel megamerge). |
| `docs/refs/openclaw/.gitkeep` | placeholder | empty |
| `docs/refs/claude-code/inventory.md` | 2026-04 | Claude-code parity tracker |
| `docs/refs/hermes-agent/inventory.md` | 2026-04 | Hermes parity tracker |
| `docs/refs/kimi-cli/inventory.md` | 2026-04 | Kimi CLI parity tracker |
| `docs/refs/phase-11-commit-list.md` | 2026-04-23 | Phase 11 squash record |
| `docs/refs/hermes-agent/2026-04-28-major-gaps.md` | **NOT PRESENT** | Memory entry referenced this — but only `inventory.md` exists under `docs/refs/hermes-agent/`. The Hermes-port plan + audit + amendments + final-review live in `docs/superpowers/plans/` (4 files dated 2026-04-28). |

`docs/superpowers/specs/` (12 files) and `docs/superpowers/plans/` (22 files) carry the design + plan history for everything from layered-awareness through the channel port and TUI uplift — useful supplements when reasoning about gaps.

---

## 11. Notable CLAUDE.md drift

CLAUDE.md §2 + §4 still claim 7 bundled extensions ("telegram, discord, anthropic-provider, openai-provider, coding-harness, dev-tools, memory-honcho") and 885 tests across 71 files — accurate as of 2026-04-24, now stale. Real numbers: **27 extensions** (gain of 20: aws-bedrock + skill-evolution + browser-control + browser-bridge + ambient-sensors + voice-mode + affect-injection + api-server + matrix + mattermost + signal + sms + imessage + whatsapp + whatsapp-bridge + email + homeassistant + slack + webhook + weather-example + oi-capability stub) and **426 test files**. Skills directory holds **55 skills**, not the original 15 from PR #9.

CLAUDE.md §4 phase-history table also stops at OI-removal (2026-04-27) and does not include: passive-education v1+v2, affect work A/B/C, layered-awareness V2.C, ambient sensors, voice mode, browser control, T2 batch skills, Hermes channel port, TUI Phase 1+2.
