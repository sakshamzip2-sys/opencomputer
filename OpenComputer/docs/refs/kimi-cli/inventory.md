# Kimi-cli inventory (for Phase 11)

Source repo: `/Users/saksham/Vscode/claude/sources/kimi-cli/`
Walked: `kimi_cli/tools/`, `kimi_cli/wire/`, `kimi_cli/agent/`, `kimi_cli/runtime/`, `kimi_cli/dmail/`, `kimi_cli/mcp/`, plus injection/subagent/compaction modules.
Date: 2026-04-22.

| Item | Kind | OC equivalent (or "missing") | Value if ported | Cost | Verdict | Destination |
|---|---|---|---|---|---|---|
| **Tools (mostly already-have)** | | | | | | |
| Agent | tool | Delegate | — | — | already-have | core |
| Shell | tool | Bash | — | — | already-have | core |
| ReadFile | tool | Read | — | — | already-have | core |
| WriteFile | tool | Write | — | — | already-have | core |
| StrReplaceFile | tool | Edit (coding-harness) | — | — | already-have | coding-harness |
| Glob / Grep | tool | already-have | — | — | already-have | core |
| SearchWeb / FetchURL | tool | WebSearch / WebFetch | — | — | already-have | core |
| SetTodoList | tool | TodoWrite (coding-harness) | — | — | already-have | coding-harness |
| TaskList / TaskOutput / TaskStop | tool | covered by Background (coding-harness) | — | — | already-have | coding-harness |
| SendDMail | tool | covered by Rewind (coding-harness) | — | — | already-have | coding-harness |
| Think | tool | missing | low | S | skip | n/a |
| AskUserQuestion | tool | missing — Phase 11b covers it | med | S | port (via 11b) | core |
| EnterPlanMode / ExitPlanMode | tool | partial — `--plan` flag exists, no tools | med | M | port | coding-harness (Phase 11b §11b) |
| **Subsystems (mostly already-have)** | | | | | | |
| KimiSoul | subsystem | partial — Runtime/Soul split = architecture-review §4.5 | high | L | already-have-partial | core |
| Compaction | subsystem | CompactionEngine | — | — | already-have | core |
| Dynamic Injection (Plan/Yolo) | pattern | already-have | — | — | already-have | core |
| Context + Checkpoints | subsystem | Rewind in coding-harness | — | — | already-have | coding-harness |
| Wire Protocol | subsystem | already-have (gateway/wire_server.py) | — | — | already-have | core |
| Subagents (LaborMarket) | subsystem | Delegate + DelegateTool | — | — | already-have | core |
| Approval Runtime | subsystem | partial — hook engine has block decision | med | M | port | core |
| Background Task Manager | subsystem | covered by Background (coding-harness) | — | — | already-have | coding-harness |
| Skills (Flow-based) | subsystem | MemoryManager.list_skills | — | — | already-have | core |
| Hook Engine | subsystem | already-have (hooks/engine.py) | — | — | already-have | core |
| LLM Provider System | subsystem | already-have (provider plugins) | — | — | already-have | core |
| OAuth Manager | subsystem | missing | med | M | port | core (pairs with hermes credential_pool) |
| Plugin Manager | subsystem | already-have (plugins/registry.py) | — | — | already-have | core |
| Slash Commands (Soul + Shell) | pattern | partial — coding-harness has `/plan` | high | M | port | core |
| **Notifications** | | | | | | |
| Notifications (Manager + Store + sinks: llm/wire/shell) | subsystem | missing | med | M | port | new:extensions/notifications |
| **MCP** | | | | | | |
| MCP CLI (add/list/auth/test) | CLI | missing — Phase 11c covers it | high | M | port | mcp-bundle (Phase 11c) |
| **UI / Channels** | | | | | | |
| Shell UI (interactive TUI) | UI adapter | missing | med | L | port-later | new:extensions/ui-shells |
| Print UI (plain text) | UI adapter | rich.Console used directly | — | — | already-have | core |
| ACP/IDE UI | channel | missing | high | L | port | mcp-bundle (with ACP) |
| Web UI + API | channel | missing — overlaps with hermes api_server | med | L | merge | new:extensions/api-server |
| **Session ops** | | | | | | |
| Session Fork | subsystem | missing | med | M | port | core |
| Import/Export Sessions | subsystem | missing | med | M | port | core |
| **Skip** | | | | | | |
| Visualizations (vis/) | subsystem | n/a | — | — | skip | n/a |

## Notes

OpenComputer already absorbed kimi's biggest patterns (wire, injection, compaction, hooks, subagents, three-layer architecture). The remaining ports group into three clusters:

1. **Slash command routing** (Soul + Shell layers). Coding-harness has ad-hoc `/plan` `/exit-plan`; a generalised slash-command dispatcher in core would let any plugin register `/<plugin>:<command>` cleanly. Pairs naturally with Phase 11c's MCP-prompts-as-slash-commands work.
2. **Notifications subsystem with pluggable sinks**. Kimi's manager + store + (llm | wire | shell) sinks gives the agent a way to fire-and-forget user-visible signals without touching the chat stream. Useful for background-task completion alerts. Effort: M.
3. **Session fork + import/export**. Fork makes "explore an alternate" workflows trivial; import/export underpins the "share a session" UX. Effort: M each.

The TUI, web UI, and ACP/IDE channels are collectively the "give OpenComputer a face other than the CLI" project. ACP is the most strategic (IDE integration), but L cost. Phase 11e candidates pending dogfood signal.

Most kimi extracts are **already-have** — the remaining list is short and focused, which validates how thoroughly CLAUDE.md's feature-map captured the high-value patterns up front.
