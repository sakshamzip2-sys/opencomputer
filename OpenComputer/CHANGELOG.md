# Changelog

All notable changes to OpenComputer are listed here. Follows [Keep a Changelog](https://keepachangelog.com/) conventions, [semver](https://semver.org/).

## [Unreleased]

### Added (Phase 10f — memory baseline completion)
- **`Memory` tool** (`opencomputer/tools/memory_tool.py`) — agent-facing
  curation of MEMORY.md + USER.md. Actions: `add`/`replace`/`remove`/`read`.
  Targets: `memory` (agent observations) / `user` (user preferences).
- **`SessionSearch` tool** (`opencomputer/tools/session_search_tool.py`) —
  agent-facing FTS5 search across all past messages. Default limit 10,
  max 50. Wraps new `SessionDB.search_messages()` returning full content.
- **USER.md support** in `MemoryManager` — separate from MEMORY.md so
  agent observations don't commingle with user preferences.
- **Atomic write pipeline** — `_write_atomic()` + `_file_lock()` (fcntl /
  msvcrt). Every mutation: acquire lock → backup to `<path>.bak` →
  write temp → `os.replace()`. Never leaves partial files.
- **Character limits** on both files, configurable via `MemoryConfig`.
  Over-limit writes raise `MemoryTooLargeError` (returned as tool error).
- **Declarative memory injected into base system prompt** (frozen per
  session) — preserves Anthropic prefix cache across turns.
  `PromptBuilder.build()` gained `declarative_memory`, `user_profile`,
  `memory_char_limit`, `user_char_limit` params.
- **`MemoryProvider` ABC** (`plugin_sdk/memory.py`) — public contract for
  external memory plugins (Honcho, Mem0, Cognee). 5 required methods,
  2 optional lifecycle hooks, cadence-aware via `turn_index`.
- **`InjectionContext.turn_index`** field (default 0, backward compatible).
- **`PluginAPI.register_memory_provider()`** with one-at-a-time guard +
  isinstance check.
- **`MemoryContext` + `MemoryBridge`** — shared deps bag + exception-safe
  orchestrator wired into `AgentLoop`. A broken provider never crashes
  the loop.
- **`opencomputer memory` CLI subcommand group** —
  `show / edit / search / stats / prune / restore` with `--user` flag.

### Changed
- `MemoryConfig` gained: `user_path`, `memory_char_limit=4000`,
  `user_char_limit=2000`, `provider=""`, `enabled=True`,
  `fallback_to_builtin=True`. Backward compatible.

### Tests
- +62 tests in `tests/test_phase10f.py`, all green.
- Full suite: 336 passing.

## [0.1.0] — 2026-04-21 (pre-alpha)

### Added
- Initial public release.
- Core agent loop with tool dispatch (`opencomputer/agent/loop.py`).
- Three-pillar memory: declarative (MEMORY.md), procedural (skills/), episodic (SQLite + FTS5 full-text search).
- 7 built-in tools: Read, Write, Bash, Grep, Glob, skill_manage, delegate.
- Strict plugin SDK boundary (`plugin_sdk/`) with manifest-first two-phase discovery.
- Bundled plugins:
  - `anthropic-provider` — Anthropic Claude models with Bearer-auth proxy support.
  - `openai-provider` — OpenAI Chat Completions + any OpenAI-compatible endpoint.
  - `telegram` — Telegram Bot API channel with typing indicators.
  - `discord` — Discord channel via discord.py.
  - `coding-harness` — Edit, MultiEdit, TodoWrite, background-process tools + plan mode.
- MCP integration — connects to Model Context Protocol servers (stdio), tools namespaced.
- Gateway for multi-channel daemons.
- Wire server — JSON over WebSocket RPC for TUI / IDE / web clients (`opencomputer wire`).
- Streaming responses (Anthropic + OpenAI) with per-turn typing indicators on Telegram.
- Dynamic injection engine — cross-cutting modes as providers (plan mode).
- Hardened context compaction — real token counts, tool-pair preservation, aux-fail fallback.
- Runtime context threading — plan_mode / yolo_mode / custom flags flow loop → hooks → delegate → subagents.
- CLI: `chat`, `gateway`, `wire`, `search`, `sessions`, `skills`, `plugins`, `setup`, `doctor`, `config`.
- Interactive setup wizard (`opencomputer setup`).
- Health check (`opencomputer doctor`).
- Typed YAML config with dotted-key get/set.
- GitHub Actions CI — pytest on Python 3.12 + 3.13, ruff lint.
- 114 tests.

### Credits
Architectural ideas synthesized from [Claude Code](https://github.com/anthropics/claude-code),
[Hermes Agent](https://github.com/NousResearch/hermes-agent),
[OpenClaw](https://github.com/openclaw/openclaw),
[Kimi CLI](https://github.com/MoonshotAI/kimi-cli).

[Unreleased]: https://github.com/sakshamzip2-sys/opencomputer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sakshamzip2-sys/opencomputer/releases/tag/v0.1.0
