# Hermes-agent inventory (for Phase 11)

Source repo: `/Users/saksham/Vscode/claude/sources/hermes-agent/`
Walked: `tools/`, `agent/`, `gateway/`, `gateway/platforms/`, `gateway/builtin_hooks/`, `cron/`, `acp_adapter/`, `environments/` (skipped wholesale — RL training).
Date: 2026-04-22.

| Item | Kind | OC equivalent (or "missing") | Value if ported | Cost | Verdict | Destination |
|---|---|---|---|---|---|---|
| **Tools** | | | | | | |
| memory_tool | tool | partial — SkillManage covers skills, MEMORY.md is plain file | high | M | port | core |
| session_search_tool | tool | partial — SessionDB.search exists, no tool wrapper | high | M | port | core |
| mcp_tool | tool | partial — MCP client works, no tool wrapper to call MCP-the-meta-tool | med | M | port | mcp-bundle (Phase 11c) |
| mcp_oauth (OAuth 2.1 client) | subsystem | missing | high | M | port | mcp-bundle (Phase 11c) |
| browser_tool | tool | missing | high | L | port | new:extensions/browser |
| todo_tool | tool | TodoWrite (coding-harness) | — | — | already-have | coding-harness |
| delegate_tool | tool | Delegate (different design) | — | — | already-have | core |
| terminal_tool | tool | Bash (different abstraction) | — | — | already-have | core |
| file_tools / file_operations | tool | Read/Write/Glob/Grep | — | — | already-have | core |
| code_execution_tool | tool | Bash | — | — | already-have | core |
| send_message_tool (cross-platform) | tool | missing | high | M | port | new:extensions/messaging |
| clarify_tool | tool | partial — overlaps with AskUserQuestion (Phase 11b) | med | S | merge | core (with 11b) |
| approval (tool gating) | tool/subsystem | partial — hook engine has block decision | med | S | port | core |
| image_generation_tool (FAL.ai multi-model) | tool | missing | med | M | port | new:extensions/image-gen |
| vision_tools (multi-modal) | tool | missing | med | M | port | new:extensions/vision |
| voice_mode / tts_tool / transcription_tools | tool | missing | low | M | skip | n/a |
| homeassistant_tool | tool | missing | low | S | skip | n/a |
| feishu_doc_tool / feishu_drive_tool | tool | missing | low | M | skip | n/a |
| osv_check (security scan) | tool | missing | low | S | skip | n/a |
| mixture_of_agents_tool | tool | partial — Delegate covers single-spawn | med | M | skip | n/a |
| **Agent subsystems** | | | | | | |
| auxiliary_client (multi-provider router) | subsystem | missing | high | M | port | core |
| credential_pool | subsystem | missing | high | M | port | core |
| smart_model_routing (cheap-route gating) | subsystem | architecture-review §4.7 (parked) | med | S | port | core (architecture §4.7 trigger) |
| context_compressor | subsystem | partial — CompactionEngine covers it | med | M | merge | core |
| context_engine (context-reference tracking) | subsystem | missing | med | M | port | core |
| prompt_caching (cached responses) | subsystem | partial — `_system_prompt_snapshot` from §3.4 covers prefix caching | high | S | merge | core |
| prompt_builder (system prompt assembly) | subsystem | already-have (PromptBuilder) | — | — | already-have | core |
| memory_manager / memory_provider | subsystem | partial — MemoryManager exists, no plugin backend | high | M | port | core |
| skill_utils (skill introspection) | subsystem | partial — list_skills covers | med | M | port | core |
| error_classifier | subsystem | missing | med | M | port | core |
| rate_limit_tracker | subsystem | missing | med | S | port | core |
| retry_utils (exp backoff) | subsystem | partial — tenacity is a dep, no wrapper | med | S | port | core |
| redact (PII / secret scrubbing) | subsystem | missing | med | S | port | core |
| insights (telemetry / analytics) | subsystem | missing | low | M | skip | n/a |
| display (terminal UI) | subsystem | rich.Console used directly | — | — | already-have | core |
| anthropic_adapter / openai_adapter | subsystem | already-have (provider plugins) | — | — | already-have | core |
| bedrock_adapter / gemini_cloudcode_adapter / google_oauth | subsystem | partial — architecture-review Phase 11e | low | M | defer | n/a |
| copilot_acp_client | subsystem | missing | low | M | skip | n/a |
| model_metadata (context-window + cost tables) | subsystem | partial — DEFAULT_CONTEXT_WINDOWS in compaction.py | high | M | port | core |
| usage_pricing (token-cost accounting) | subsystem | missing | med | M | port | core |
| path_security / url_safety / tirith_security | subsystem | missing | med | M | port | core |
| **Channels (gateway/platforms)** | | | | | | |
| discord | channel | already-have | — | — | already-have | core |
| telegram | channel | already-have | — | — | already-have | core |
| slack | channel | missing | high | M | port | new:extensions/slack |
| matrix | channel | missing | med | M | port | new:extensions/matrix |
| email | channel | missing | high | M | port | new:extensions/email |
| webhook (generic) | channel | missing | med | M | port | new:extensions/webhook |
| api_server (OpenAI-compat HTTP) | channel | missing | high | L | port | new:extensions/api-server |
| weixin / wecom / dingtalk / feishu / mattermost / signal / whatsapp / sms / bluebubbles / qqbot / zalo | channel | missing | low | M each | skip | n/a (Phase 11e candidates if demand surfaces) |
| **Cron** | | | | | | |
| scheduler / jobs (cron with per-task isolation, FS streaming) | cron | missing | high | M | port | core |
| **MCP / ACP** | | | | | | |
| acp_adapter / acp_server (Agent Client Protocol) | MCP | missing | high | L | port | mcp-bundle |
| **Memory plugins (bundled)** | | | | | | |
| plugin:memory (pluggable memory backends) | plugin | missing | high | M | port | core (with memory_manager) |
| plugin:context_engine | plugin | missing | med | M | port | core |
| plugin:holographic / plugin:honcho / plugin:mem0 / plugin:retaindb / plugin:supermemory | plugin (memory backends) | missing | low | M each | skip | n/a (port the ABC, leave specific backends to user) |
| **Out of scope** | | | | | | |
| environments/* (RL benchmark scaffolds) | infra | n/a | — | — | skip | n/a |
| tool_call_parsers/* (training infra) | infra | n/a | — | — | skip | n/a |
| benchmarks/yc_bench, terminalbench_2, tblite | infra | n/a | — | — | skip | n/a |

## Notes

**Highest-ROI extracts**:
1. **Auxiliary client + credential pool**. Hermes's multi-provider router handles OpenRouter + Nous + Codex OAuth + 7 direct providers with automatic fallback on credit exhaustion. OpenComputer's provider system is plugin-singleton-per-process; the credential-pool pattern is a strict upgrade for users who hit rate limits. Effort: 2-3 days.
2. **Context-window + pricing tables (`model_metadata`)**. Centralised lookup of (context_window, input_cost, output_cost) per model. Compaction already needs context_window — this is the broader version. Lives in core; compaction migrates to use it. Effort: 1 day.
3. **Cron / scheduler**. Persistent job storage + croniter scheduling + per-task isolation. Maps cleanly onto OpenComputer's daemon mode. Effort: 2-3 days. Belongs in core.
4. **MCP OAuth 2.1 client + http transport** (Phase 11c hooks).
5. **API server adapter** (OpenAI-compat HTTP). Lets any OpenAI-SDK app use OpenComputer as a backend. Big strategic value but L cost.

**Channels to skip (for now)**: Asia-region channels (weixin/wecom/feishu/dingtalk/zalo) and macOS-specific bridges (bluebubbles) are low-novelty and have niche audiences. Phase 11e gates these on dogfood demand.

**Memory plugin layer**: hermes ships 5 vector-DB-backed memory backends as separate plugins. Port the ABC + plugin discovery surface; do not bundle any specific backend (matches OpenComputer's "don't default-install" rule).
