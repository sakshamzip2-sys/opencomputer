# Changelog

All notable changes to OpenComputer are listed here. Follows [Keep a Changelog](https://keepachangelog.com/) conventions, [semver](https://semver.org/).

## [Unreleased]

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
