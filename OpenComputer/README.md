# OpenComputer

A personal AI agent framework — plugin-first, self-improving, multi-channel.

A synthesis of the best ideas from [Claude Code](https://github.com/anthropics/claude-code), [Hermes Agent](https://github.com/NousResearch/hermes-agent), [OpenClaw](https://github.com/openclaw/openclaw), and [Kimi CLI](https://github.com/MoonshotAI/kimi-cli).

## What it does

- **Chat agent** with tool calling (file ops, bash, grep, glob, subagents, skills).
- **Three-pillar persistent memory:** declarative (MEMORY.md), procedural (skills/), episodic (SQLite + FTS5 full-text search).
- **Self-improvement loop:** the agent saves complex workflows as skills that auto-activate next time.
- **Strict plugin SDK boundary:** third-party plugins never import core internals, so the core can evolve without breaking plugins.
- **Multi-channel gateway:** run as a daemon; chat via Telegram and Discord today, Slack coming.
- **Multiple providers:** Anthropic (native + proxy-compatible), OpenAI, any OpenAI-compatible endpoint (OpenRouter, Ollama, etc.).
- **MCP integration:** plug in any [Model Context Protocol](https://modelcontextprotocol.io) server — its tools become native tools.

## Status

Pre-alpha (0.1.0). Core architecture stable. 114 tests passing. Adding features incrementally.

## Install

Requires **Python 3.12+**.

```bash
pip install opencomputer
```

### For development

```bash
git clone https://github.com/sakshamzip2-sys/opencomputer.git
cd opencomputer
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Quickstart

```bash
# 1. Run the setup wizard — picks provider, saves config
opencomputer setup

# 2. Export your API key (setup tells you which)
export ANTHROPIC_API_KEY=sk-ant-...
# or: export OPENAI_API_KEY=sk-...

# 3. Verify the install
opencomputer doctor

# 4. Chat
opencomputer
```

## Commands

```bash
opencomputer                 # start a chat session (alias for `chat`)
opencomputer chat            # interactive REPL with tools
opencomputer gateway         # run the daemon — listens on configured channels
opencomputer search QUERY    # full-text search past conversations
opencomputer sessions        # list recent sessions
opencomputer skills          # list available skills (bundled + user)
opencomputer plugins         # list installed plugins
opencomputer setup           # first-run wizard
opencomputer doctor          # diagnose config/env issues
opencomputer config show     # print effective config
opencomputer config get KEY  # read one config value (e.g. model.provider)
opencomputer config set KEY VALUE
```

## Coding mode

OpenComputer ships with a `coding-harness` plugin that adds Claude-Code-style
coding tools (Edit, MultiEdit, TodoWrite, background process management) plus
a formal "plan mode" that refuses destructive tools while you review the plan.

```bash
# Normal mode — Edit/Write/Bash work, agent can modify files directly
opencomputer

# Plan mode — agent describes what it would do, Edit/Write/Bash are refused
# Useful for big refactors where you want to review before committing
opencomputer chat --plan

# Disable automatic context compaction (debugging long sessions)
opencomputer chat --no-compact
```

In plan mode, plan-mode guidance is injected into the system prompt AND a
PreToolUse hook hard-blocks destructive tools — belt + suspenders. Subagents
spawned via the `delegate` tool inherit plan mode automatically.

Remove the coding harness any time by removing or renaming
`extensions/coding-harness/`. The core agent stays fully functional.

## Messaging channels

### Telegram

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → get a token
2. `export TELEGRAM_BOT_TOKEN=123:ABC...`
3. `opencomputer gateway` — the bundled Telegram plugin auto-connects
4. DM your bot on Telegram

## MCP servers

Plug any MCP server into OpenComputer. Edit `~/.opencomputer/config.yaml`:

```yaml
mcp:
  servers:
    - name: my-server
      transport: stdio
      command: python3
      args:
        - /path/to/mcp_server.py
      enabled: true
```

The server's tools become available to the agent on next run (namespaced `my-server__tool_name`).

## Architecture

```
opencomputer/   (core — agent loop, state, memory, tools, hooks, gateway, plugin discovery)
plugin_sdk/     (public contract — stable types plugins import)
extensions/     (bundled plugins — telegram, discord, anthropic-provider, openai-provider, coding-harness)
```

**Key design rule:** extensions import only from `plugin_sdk/*`, never from `opencomputer/*`. The core can be refactored freely without breaking plugins.

## Writing a plugin

Plugins are separate folders with a manifest and an entry module. Minimal channel plugin:

```
extensions/my-channel/
├── plugin.json           # { "id": "my-channel", "version": "0.1.0", "entry": "plugin", "kind": "channel" }
├── plugin.py             # exports register(api) — registers the adapter
└── adapter.py            # your BaseChannelAdapter subclass
```

See `extensions/telegram/` for a working reference.

## License

MIT — see `LICENSE.md`.

## Credits

Architectural ideas synthesized from:
- **Claude Code** — plugin primitives vocabulary (commands/skills/agents/hooks/MCP), lifecycle events
- **Hermes Agent** — Python core patterns, three-pillar memory, agent loop shape
- **OpenClaw** — plugin-first architecture, manifest-first discovery, strict SDK boundary
- **Kimi CLI** — dynamic injection, fire-and-forget hooks, deferred MCP, wire-protocol UI decoupling
